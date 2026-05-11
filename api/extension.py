"""Scheduler, Redis-locking, and task-run logging primitives."""

import json
import logging
import os
import sys
import time
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from typing import Any, Callable, Self


# ── sys.path bootstrap ──────────────────────────
def _find_root(marker: str = "project_root.txt") -> Path:
    try:
        start = Path(__file__).resolve().parent
    except NameError:  # REPL / python -c / exec()
        start = Path.cwd().resolve()
    for p in (start, *start.parents):
        if (p / marker).is_file():
            return p
    raise RuntimeError(f"{marker!r} not found above {start}")


ROOT_DIR = _find_root()
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))
# ─────────────────────────────────────

log = logging.getLogger(__name__)


def parse_bool(value: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"1", "true", "t", "yes", "y", "on"}:
        return True
    if normalized in {"0", "false", "f", "no", "n", "off"}:
        return False
    raise ValueError(f"Invalid boolean value: {value!r}")


@dataclass(slots=True)
class ApiRedisConfig:
    host: str = "localhost"
    port: int = 6379
    db: int = 0
    lock_db: int = 1
    password: str | None = None
    ssl: bool = False
    socket_timeout: float = 2.0
    lock_ttl: int = 1200
    jobstore_key_prefix: str = "api_skewnono:jobs:"
    lock_key_prefix: str = "api_skewnono:lock:"
    log_list_key: str = "api_skewnono:logs:tasks"
    log_list_max: int = 500
    heartbeat_key: str = "api_skewnono:scheduler:heartbeat"
    heartbeat_interval: int = 30
    heartbeat_ttl: int = 120
    extra_client_kwargs: dict[str, Any] = field(default_factory=dict)

    def to_lock_client_kwargs(self) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "host": self.host,
            "port": self.port,
            "db": self.lock_db,
            "ssl": self.ssl,
            "socket_timeout": self.socket_timeout,
            "decode_responses": True,
        }
        if self.password:
            kwargs["password"] = self.password
        kwargs.update(self.extra_client_kwargs)
        return kwargs

    @classmethod
    def from_env(cls, **overrides: Any) -> Self:
        values: dict[str, Any] = {}

        host = os.getenv("API_REDIS_HOST")
        if host:
            values["host"] = host

        port = os.getenv("API_REDIS_PORT")
        if port:
            values["port"] = int(port)

        db = os.getenv("API_REDIS_DB")
        if db:
            values["db"] = int(db)

        lock_db = os.getenv("API_REDIS_LOCK_DB")
        if lock_db:
            values["lock_db"] = int(lock_db)

        password = os.getenv("API_REDIS_PASSWORD")
        if password is not None:
            values["password"] = password or None

        ssl = os.getenv("API_REDIS_SSL")
        if ssl is not None:
            values["ssl"] = parse_bool(ssl)

        socket_timeout = os.getenv("API_REDIS_TIMEOUT")
        if socket_timeout:
            values["socket_timeout"] = float(socket_timeout)

        lock_ttl = os.getenv("API_REDIS_LOCK_TTL")
        if lock_ttl:
            values["lock_ttl"] = int(lock_ttl)

        log_list_max = os.getenv("API_REDIS_LOG_MAX")
        if log_list_max:
            values["log_list_max"] = int(log_list_max)

        values.update(overrides)
        return cls(**values)


def load_api_redis_config(**overrides: Any) -> ApiRedisConfig:
    return ApiRedisConfig.from_env(**overrides)


def _redis_client_class() -> type[Any]:
    from redis import Redis

    return Redis


def create_lock_client(
    config: ApiRedisConfig | None = None,
    **overrides: Any,
) -> Any:
    if config is None:
        config = load_api_redis_config(**overrides)
    elif overrides:
        config = replace(config, **overrides)
    return _redis_client_class()(**config.to_lock_client_kwargs())


from flask_apscheduler import APScheduler

scheduler = APScheduler()


def configure_scheduler(app: Any, config: ApiRedisConfig) -> None:
    from apscheduler.jobstores.redis import RedisJobStore

    # Forward the full connection posture (ssl, timeout, extras) so the
    # job-store client matches the lock client. Otherwise locks + heartbeat
    # speak TLS while the job store tries a bare TCP connection on the same
    # host and silently fails to load any jobs.
    store_kwargs: dict[str, Any] = {
        "host": config.host,
        "port": config.port,
        "db": config.db,
        "ssl": config.ssl,
        "socket_timeout": config.socket_timeout,
        "jobs_key": f"{config.jobstore_key_prefix}jobs",
        "run_times_key": f"{config.jobstore_key_prefix}run_times",
    }
    if config.password:
        store_kwargs["password"] = config.password
    store_kwargs.update(config.extra_client_kwargs)

    app.config["SCHEDULER_JOBSTORES"] = {"default": RedisJobStore(**store_kwargs)}
    app.config["SCHEDULER_JOB_DEFAULTS"] = {
        "coalesce": True,
        "max_instances": 1,
        "misfire_grace_time": 60,
    }
    app.config["SCHEDULER_TIMEZONE"] = "Asia/Seoul"
    app.config["SCHEDULER_API_ENABLED"] = False


class TaskLogger:
    """Pushes task-run records onto a Redis list for the dashboard.

    Pipelines lpush + ltrim into a single round-trip. Failures to push are
    swallowed (with stacktrace) so observability never breaks the task itself.
    """

    __slots__ = ("client", "key", "max_records")

    def __init__(self, client: Any, key: str, max_records: int) -> None:
        self.client = client
        self.key = key
        self.max_records = max_records

    def record(self, job: str, event: str, **extra: Any) -> None:
        rec: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "job": job,
            "event": event,
        }
        rec.update(extra)
        log.info("task-run %s", rec)
        try:
            with self.client.pipeline() as pipe:
                pipe.lpush(self.key, json.dumps(rec))
                pipe.ltrim(self.key, 0, self.max_records - 1)
                pipe.execute()
        except Exception:
            log.exception("failed to push task-run record")

    def wrap(self, fn: Callable) -> Callable:
        """Emit start / end / error records around each invocation of ``fn``."""

        @wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            self.record(fn.__name__, "start")
            t0 = time.monotonic()
            try:
                result = fn(*args, **kwargs)
            except Exception as exc:
                self.record(
                    fn.__name__,
                    "error",
                    duration_ms=int((time.monotonic() - t0) * 1000),
                    error=repr(exc),
                )
                raise
            self.record(
                fn.__name__,
                "end",
                duration_ms=int((time.monotonic() - t0) * 1000),
            )
            return result

        return wrapper


def read_task_logs(client: Any, key: str, *, limit: int = 200) -> list[dict[str, Any]]:
    raw = client.lrange(key, 0, limit - 1)
    out: list[dict[str, Any]] = []
    for item in raw:
        try:
            out.append(json.loads(item))
        except (ValueError, TypeError):
            log.warning("dropping malformed task-log entry: %r", item)
            continue
    return out


# Compare-and-delete: only release the lock if we still own it. Without this
# guard a task that overruns its TTL would DEL whoever acquired the key next,
# breaking mutual exclusion.
_LOCK_RELEASE_SCRIPT = (
    "if redis.call('get', KEYS[1]) == ARGV[1] then "
    "  return redis.call('del', KEYS[1]) "
    "else "
    "  return 0 "
    "end"
)

# Module-level Script so redis-py caches the SHA and uses EVALSHA + NOSCRIPT
# fallback automatically. Bound at call time via the ``client=`` kwarg.
_release_lock: Any = None


def _get_release_script(client: Any) -> Any:
    global _release_lock
    if _release_lock is None:
        _release_lock = client.register_script(_LOCK_RELEASE_SCRIPT)
    return _release_lock


def redis_lock(
    client: Any,
    *,
    key: str,
    ttl: int,
    on_skip: Callable[[str], None] | None = None,
) -> Callable[[Callable], Callable]:
    """Skip-if-held lock with owner-checked release.

    Each acquisition mints a fresh token stored as the key's value. Release
    runs a Lua CAS so we only DEL the key when we still hold it. Optional
    ``on_skip(fn_name)`` fires when another holder already has the lock.
    """
    from redis.exceptions import RedisError

    release = _get_release_script(client)

    def decorator(fn: Callable) -> Callable:
        @wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            token = uuid.uuid4().hex
            if not client.set(key, token, nx=True, ex=ttl):
                if on_skip is not None:
                    on_skip(fn.__name__)
                return None
            try:
                return fn(*args, **kwargs)
            finally:
                try:
                    release(keys=[key], args=[token], client=client)
                except RedisError:
                    log.exception("failed to release lock %s", key)

        return wrapper

    return decorator
