"""Scheduler, Redis-locking, and task-run logging primitives."""

import json
import logging
import os
import socket
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from functools import wraps
from pathlib import Path
from typing import Any, Callable


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


@dataclass(slots=True)
class ApiRedisConfig:
    host: str = "localhost"
    port: int = 6379
    db: int = 0
    lock_db: int = 1
    password: str | None = None
    ssl: bool = False
    socket_timeout: float = 2.0
    # Orphan-clear window, NOT a runtime budget. A live run re-arms its own
    # TTL every ``lock_ttl // 3`` seconds (see _renew_until_stopped), so this
    # only bounds how long a lock survives a process that died without
    # running its release (OOM kill, uWSGI harakiri, host maintenance).
    # Shorter = fewer wasted `lock held` skips after a crash; too short risks
    # expiring under a starved renewal thread. 300s renews every 100s.
    lock_ttl: int = 300
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


def _redis_client_class() -> type[Any]:
    from redis import Redis

    return Redis


def create_lock_client(
    config: ApiRedisConfig | None = None,
    **overrides: Any,
) -> Any:
    if config is None:
        config = ApiRedisConfig()
    if overrides:
        config = replace(config, **overrides)
    return _redis_client_class()(**config.to_lock_client_kwargs())


from flask_apscheduler import APScheduler

scheduler = APScheduler()


def configure_scheduler(app: Any, config: ApiRedisConfig) -> None:
    from apscheduler.executors.pool import ThreadPoolExecutor
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
    # 2 CPU / 8 GiB cloud env, 4 uWSGI workers → ~2 GiB per worker. Cap the
    # scheduler thread pool at 4 so the worker hosting it (worker_id=1) keeps
    # headroom for HTTP traffic and doesn't OOM when long jobs (10-20 min,
    # pandas/OpenSearch heavy) overlap.
    #
    # "fast" is a separate single-thread lane for sub-minute jobs. Sharing
    # "default" would let four concurrent long jobs starve a 15s job for
    # 10+ minutes, and coalesce=True silently drops the missed fires.
    app.config["SCHEDULER_EXECUTORS"] = {
        "default": ThreadPoolExecutor(max_workers=4),
        "fast": ThreadPoolExecutor(max_workers=1),
    }
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


def read_task_logs(
    client: Any,
    key: str,
    *,
    limit: int = 200,
    max_age_seconds: int | None = None,
) -> list[dict[str, Any]]:
    raw = client.lrange(key, 0, limit - 1)
    cutoff: datetime | None = None
    if max_age_seconds is not None:
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=max_age_seconds)
    out: list[dict[str, Any]] = []
    for item in raw:
        try:
            rec = json.loads(item)
        except (ValueError, TypeError):
            log.warning("dropping malformed task-log entry: %r", item)
            continue
        if cutoff is not None and _record_is_older_than(rec, cutoff):
            continue
        out.append(rec)
    return out


def _record_is_older_than(rec: dict[str, Any], cutoff: datetime) -> bool:
    # Lenient: an unparseable / missing ts keeps the record visible. Same
    # policy as malformed JSON above — observability never hides data.
    ts_str = rec.get("ts")
    if not isinstance(ts_str, str):
        return False
    try:
        ts = datetime.fromisoformat(ts_str)
    except ValueError:
        return False
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts < cutoff


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

# Same CAS guard, but pushes the expiry out instead of deleting. Returned 0
# means we no longer own the key, so the caller must stop renewing.
_LOCK_RENEW_SCRIPT = (
    "if redis.call('get', KEYS[1]) == ARGV[1] then "
    "  return redis.call('expire', KEYS[1], ARGV[2]) "
    "else "
    "  return 0 "
    "end"
)

# Module-level Scripts so redis-py caches the SHA and uses EVALSHA + NOSCRIPT
# fallback automatically. Bound at call time via the ``client=`` kwarg.
_release_lock: Any = None
_renew_lock: Any = None


def _get_release_script(client: Any) -> Any:
    global _release_lock
    if _release_lock is None:
        _release_lock = client.register_script(_LOCK_RELEASE_SCRIPT)
    return _release_lock


def _get_renew_script(client: Any) -> Any:
    global _renew_lock
    if _renew_lock is None:
        _renew_lock = client.register_script(_LOCK_RENEW_SCRIPT)
    return _renew_lock


def lock_owner_token() -> str:
    """Mint this acquisition's lock value: identity plus a uniqueness nonce.

    Doubles as the CAS token — ``_LOCK_RELEASE_SCRIPT`` compares the stored
    value byte-for-byte, so any unique string works, and packing the holder's
    identity in means a contender that loses the race can report *who* beat
    it instead of a bare "lock held". ``sort_keys`` keeps the encoding stable.
    """
    return json.dumps(
        {
            "token": uuid.uuid4().hex,
            "host": socket.gethostname(),
            "pid": os.getpid(),
            "acquired": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        },
        sort_keys=True,
    )


def describe_lock_holder(client: Any, key: str) -> dict[str, Any]:
    """Read who currently holds ``key`` and how much of its TTL is left.

    Called on the skip path so a dashboard ``lock held`` row is
    self-diagnosing: an orphan from a dead process shows a ``holder`` whose
    pid is gone and a ``held_since`` far in the past, while genuine
    contention shows a live peer that acquired moments ago.

    Returns ``{}`` if Redis is unreachable — a skip record must still be
    written. ``ttl_remaining`` of -2 means the key vanished between the
    failed SET and this read (the holder finished in the gap).
    """
    try:
        with client.pipeline() as pipe:
            pipe.get(key)
            pipe.ttl(key)
            raw, ttl_remaining = pipe.execute()
    except Exception:
        log.exception("failed to read lock holder for %s", key)
        return {}

    info: dict[str, Any] = {"ttl_remaining": ttl_remaining}
    try:
        owner = json.loads(raw) if raw else None
    except (ValueError, TypeError):
        owner = None
    if isinstance(owner, dict):
        info["holder"] = f"{owner.get('host')}:{owner.get('pid')}"
        info["held_since"] = owner.get("acquired")
    return info


def _renew_until_stopped(
    client: Any,
    key: str,
    token: str,
    ttl: int,
    stop: threading.Event,
    renew: Any,
) -> None:
    """Re-arm ``key``'s TTL every ``ttl // 3`` seconds until ``stop`` is set.

    This is what decouples ``ttl`` from job runtime. Without it, ``ttl`` has
    to be a bet on how long the task takes: bet low and the key expires
    mid-run so the next fire acquires cleanly and runs *concurrently* — the
    lock silently stops protecting; bet high and one hard kill orphans the
    key for the full ``ttl``. Renewing means a live run holds the lock for
    as long as it needs while ``ttl`` shrinks to just the orphan window.

    A renewal returning 0 means we lost ownership (the key already expired
    and someone else took it). Stop immediately — the release CAS in the
    wrapper's ``finally`` will correctly no-op too, so we never delete the
    new owner's lock.

    ``renew`` is passed in rather than looked up here: every concurrent job
    runs one of these threads, and resolving the cached ``Script`` on the
    main thread at decoration time keeps them off the module global.
    """
    interval = max(ttl // 3, 1)
    while not stop.wait(interval):
        try:
            if not renew(keys=[key], args=[token, ttl], client=client):
                log.warning("lock %s no longer owned; stopping renewal", key)
                return
        except Exception:
            # Transient Redis blips shouldn't kill the watchdog — the next
            # tick retries, and there is still ``ttl`` of runway left.
            log.exception("failed to renew lock %s", key)


def redis_lock(
    client: Any,
    *,
    key: str,
    ttl: int,
    on_skip: Callable[[str, dict[str, Any]], None] | None = None,
) -> Callable[[Callable], Callable]:
    """Skip-if-held lock with owner-checked release and TTL renewal.

    Each acquisition mints a fresh owner token (see :func:`lock_owner_token`)
    stored as the key's value. While the wrapped function runs, a daemon
    watchdog thread re-arms the TTL, so ``ttl`` bounds only how long an
    orphaned lock outlives a killed process — not how long the job may take.
    Release runs a Lua CAS so we only DEL the key when we still hold it.

    Optional ``on_skip(fn_name, holder_info)`` fires when another holder
    already has the lock; ``holder_info`` comes from
    :func:`describe_lock_holder`.
    """
    from redis.exceptions import RedisError

    release = _get_release_script(client)
    renew = _get_renew_script(client)

    def decorator(fn: Callable) -> Callable:
        @wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            token = lock_owner_token()
            if not client.set(key, token, nx=True, ex=ttl):
                if on_skip is not None:
                    on_skip(fn.__name__, describe_lock_holder(client, key))
                return None
            stop = threading.Event()
            keeper = threading.Thread(
                target=_renew_until_stopped,
                args=(client, key, token, ttl, stop, renew),
                name=f"lock-renew:{key}",
                daemon=True,
            )
            keeper.start()
            try:
                return fn(*args, **kwargs)
            finally:
                # Set first: the watchdog must not re-arm a key we are about
                # to delete, or a crash right after release would leave a
                # renewed orphan behind.
                stop.set()
                try:
                    release(keys=[key], args=[token], client=client)
                except RedisError:
                    log.exception("failed to release lock %s", key)

        return wrapper

    return decorator
