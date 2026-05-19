"""Redis-backed queue helpers for hourly item checks.

The two-day refresh job should call load_generation() after it builds the new
item list. The hourly Airflow DAG calls run_check_chunk() and processes a
bounded batch from the current generation.
"""

import importlib
import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Callable

log = logging.getLogger(__name__)

ItemHandler = Callable[[str], Any]


@dataclass(slots=True)
class ItemCheckConfig:
    key_prefix: str = "item_check"
    batch_size: int = 300
    lease_seconds: int = 7200
    max_retries: int = 3
    redis_url: str | None = None
    redis_host: str = "localhost"
    redis_port: int = 6379
    redis_db: int = 0
    redis_password: str | None = None
    redis_ssl: bool = False
    redis_timeout: float = 5.0
    handler_path: str | None = None

    @classmethod
    def from_env(cls) -> "ItemCheckConfig":
        return cls(
            key_prefix=os.getenv("ITEM_CHECK_KEY_PREFIX", "item_check"),
            batch_size=int(os.getenv("ITEM_CHECK_BATCH_SIZE", "300")),
            lease_seconds=int(os.getenv("ITEM_CHECK_LEASE_SECONDS", "7200")),
            max_retries=int(os.getenv("ITEM_CHECK_MAX_RETRIES", "3")),
            redis_url=os.getenv("ITEM_CHECK_REDIS_URL") or os.getenv("REDIS_URL"),
            redis_host=(
                os.getenv("ITEM_CHECK_REDIS_HOST")
                or os.getenv("REDIS_HOST")
                or "localhost"
            ),
            redis_port=int(
                os.getenv("ITEM_CHECK_REDIS_PORT")
                or os.getenv("REDIS_PORT")
                or "6379"
            ),
            redis_db=int(
                os.getenv("ITEM_CHECK_REDIS_DB") or os.getenv("REDIS_DB") or "0"
            ),
            redis_password=(
                os.getenv("ITEM_CHECK_REDIS_PASSWORD") or os.getenv("REDIS_PASSWORD")
            ),
            redis_ssl=_env_bool(
                os.getenv("ITEM_CHECK_REDIS_SSL") or os.getenv("REDIS_SSL")
            ),
            redis_timeout=float(
                os.getenv("ITEM_CHECK_REDIS_TIMEOUT")
                or os.getenv("REDIS_TIMEOUT")
                or "5"
            ),
            handler_path=os.getenv("ITEM_CHECK_HANDLER"),
        )


def _env_bool(raw: str | None) -> bool:
    if raw is None:
        return False
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def redis_client_from_env(config: ItemCheckConfig | None = None) -> Any:
    cfg = config or ItemCheckConfig.from_env()
    from redis import Redis

    if cfg.redis_url:
        return Redis.from_url(
            cfg.redis_url,
            decode_responses=True,
            socket_timeout=cfg.redis_timeout,
        )
    return Redis(
        host=cfg.redis_host,
        port=cfg.redis_port,
        db=cfg.redis_db,
        password=cfg.redis_password,
        ssl=cfg.redis_ssl,
        decode_responses=True,
        socket_timeout=cfg.redis_timeout,
    )


def load_handler(config: ItemCheckConfig | None = None) -> ItemHandler:
    cfg = config or ItemCheckConfig.from_env()
    if not cfg.handler_path:
        raise RuntimeError(
            "ITEM_CHECK_HANDLER must be set to 'module:function' before "
            "running item checks"
        )
    module_name, sep, func_name = cfg.handler_path.partition(":")
    if not sep:
        raise RuntimeError("ITEM_CHECK_HANDLER must use 'module:function' format")
    module = importlib.import_module(module_name)
    handler = getattr(module, func_name)
    return handler


class ItemCheckQueue:
    def __init__(self, client: Any, config: ItemCheckConfig | None = None) -> None:
        self.client = client
        self.config = config or ItemCheckConfig.from_env()

    def key(self, *parts: str) -> str:
        return ":".join((self.config.key_prefix, *parts))

    def gen_key(self, generation: str, name: str) -> str:
        return self.key(generation, name)

    def load_generation(self, generation: str, items: list[str]) -> dict:
        keys = [
            self.gen_key(generation, "pending"),
            self.gen_key(generation, "processing"),
            self.gen_key(generation, "done"),
            self.gen_key(generation, "failed"),
            self.gen_key(generation, "retries"),
            self.gen_key(generation, "errors"),
            self.gen_key(generation, "results"),
            self.gen_key(generation, "meta"),
        ]
        if keys:
            self.client.delete(*keys)
        if items:
            self.client.rpush(self.gen_key(generation, "pending"), *items)

        now = int(time.time())
        self.client.hset(
            self.gen_key(generation, "meta"),
            mapping={
                "generation": generation,
                "created_at": str(now),
                "total": str(len(items)),
                "status": "active",
            },
        )
        self.client.set(self.key("current_generation"), generation)
        return self.summarize(generation)

    def current_generation(self) -> str | None:
        value = self.client.get(self.key("current_generation"))
        if value is None:
            return None
        return str(value)

    def reclaim_expired(self, generation: str, now: int | None = None) -> int:
        current = now or int(time.time())
        processing_key = self.gen_key(generation, "processing")
        pending_key = self.gen_key(generation, "pending")
        expired = self.client.zrangebyscore(processing_key, 0, current)
        if not expired:
            return 0

        self.client.zrem(processing_key, *expired)
        self.client.rpush(pending_key, *expired)
        self.client.hset(
            self.gen_key(generation, "meta"),
            mapping={"last_reclaimed_at": str(current)},
        )
        return len(expired)

    def claim(self, generation: str, count: int | None = None) -> list[str]:
        limit = count or self.config.batch_size
        pending_key = self.gen_key(generation, "pending")
        raw_items = self.client.lpop(pending_key, limit)
        items = _normalize_lpop(raw_items)
        if not items:
            return []

        deadline = int(time.time()) + self.config.lease_seconds
        self.client.zadd(
            self.gen_key(generation, "processing"),
            {item: deadline for item in items},
        )
        self.client.hset(
            self.gen_key(generation, "meta"),
            mapping={"last_claimed_at": str(int(time.time()))},
        )
        return items

    def mark_done(self, generation: str, item: str, result: Any = None) -> None:
        self.client.zrem(self.gen_key(generation, "processing"), item)
        self.client.sadd(self.gen_key(generation, "done"), item)
        self.client.hdel(self.gen_key(generation, "retries"), item)
        if result is not None:
            self.client.hset(self.gen_key(generation, "results"), item, str(result))

    def mark_failed(self, generation: str, item: str, error: str) -> str:
        self.client.zrem(self.gen_key(generation, "processing"), item)
        retries = self.client.hincrby(self.gen_key(generation, "retries"), item, 1)
        self.client.hset(self.gen_key(generation, "errors"), item, error)
        if retries >= self.config.max_retries:
            self.client.sadd(self.gen_key(generation, "failed"), item)
            return "failed"

        self.client.rpush(self.gen_key(generation, "pending"), item)
        return "retry"

    def summarize(self, generation: str) -> dict:
        return {
            "generation": generation,
            "pending": self.client.llen(self.gen_key(generation, "pending")),
            "processing": self.client.zcard(self.gen_key(generation, "processing")),
            "done": self.client.scard(self.gen_key(generation, "done")),
            "failed": self.client.scard(self.gen_key(generation, "failed")),
            "meta": self.client.hgetall(self.gen_key(generation, "meta")),
        }


def _normalize_lpop(raw_items: Any) -> list[str]:
    if raw_items is None:
        return []
    if isinstance(raw_items, list):
        return [str(item) for item in raw_items]
    return [str(raw_items)]


def run_check_chunk(
    chunk_index: int,
    *,
    generation: str | None = None,
    use_current_generation: bool = True,
    config: ItemCheckConfig | None = None,
    client: Any | None = None,
    handler: ItemHandler | None = None,
) -> dict:
    cfg = config or ItemCheckConfig.from_env()
    redis_client = client or redis_client_from_env(cfg)
    queue = ItemCheckQueue(redis_client, cfg)
    if generation is None and use_current_generation:
        generation = queue.current_generation()
    if generation is None:
        log.info("chunk=%d skipped: no current generation", chunk_index)
        return {
            "chunk_index": chunk_index,
            "generation": None,
            "claimed": 0,
            "ok": 0,
            "retry": 0,
            "failed": 0,
            "reclaimed": 0,
        }

    item_handler = handler or load_handler(cfg)
    reclaimed = queue.reclaim_expired(generation)
    items = queue.claim(generation, cfg.batch_size)

    ok = 0
    retry = 0
    failed = 0
    for item in items:
        try:
            result = item_handler(item)
        except Exception as exc:
            log.exception("chunk=%d item=%s failed", chunk_index, item)
            state = queue.mark_failed(
                generation,
                item,
                f"{type(exc).__name__}: {exc}",
            )
            if state == "failed":
                failed += 1
            else:
                retry += 1
        else:
            queue.mark_done(generation, item, result)
            ok += 1

    summary = queue.summarize(generation)
    run_summary = {
        "chunk_index": chunk_index,
        "generation": generation,
        "claimed": len(items),
        "ok": ok,
        "retry": retry,
        "failed": failed,
        "reclaimed": reclaimed,
        "remaining": summary["pending"] + summary["processing"],
    }
    log.info("item check chunk summary: %s", run_summary)
    return run_summary
