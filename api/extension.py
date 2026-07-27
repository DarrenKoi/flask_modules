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


def utc_stamp() -> str:
    """Second-precision UTC ISO timestamp — the one time format records use.

    Everything this package writes to Redis is stamped in aware UTC; the
    dashboard converts to KST at render time (see ``toKst`` in index.html).
    Storing UTC and displaying local keeps records comparable across hosts
    while operators still read Seoul time.
    """
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(slots=True)
class ApiRedisConfig:
    host: str = "10.156.133.129"
    port: int = 10108
    # Everything this app stores is namespaced by the key prefixes below, so
    # one logical db holds jobs, locks, logs and heartbeat without collision.
    # A second db would only add a dependency on multi-db support, which
    # Redis Cluster does not have (SELECT is rejected there) and which the
    # Redis maintainers treat as a legacy feature.
    db: int = 0
    password: str | None = "skewRedis!2"
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

    def to_client_kwargs(self, *, decode_responses: bool) -> dict[str, Any]:
        """Connection posture shared by the lock client and the job store.

        One builder for both so they cannot drift: if only one of them learns
        about ``ssl``, locks and heartbeat speak TLS while the job store tries
        bare TCP against the same host and silently loads no jobs.

        ``decode_responses`` is the one honest difference — the lock client
        reads str, the job store reads pickled bytes — so it's a parameter
        rather than a second copy of this dict.
        """
        kwargs: dict[str, Any] = {
            "host": self.host,
            "port": self.port,
            "db": self.db,
            "ssl": self.ssl,
            "socket_timeout": self.socket_timeout,
            "decode_responses": decode_responses,
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
    return _redis_client_class()(**config.to_client_kwargs(decode_responses=True))


from flask_apscheduler import APScheduler

scheduler = APScheduler()


def configure_scheduler(app: Any, config: ApiRedisConfig) -> None:
    from apscheduler.executors.pool import ThreadPoolExecutor
    from apscheduler.jobstores.redis import RedisJobStore

    # decode_responses=False: RedisJobStore reads back pickled job bytes.
    store_kwargs = config.to_client_kwargs(decode_responses=False)
    store_kwargs["jobs_key"] = f"{config.jobstore_key_prefix}jobs"
    store_kwargs["run_times_key"] = f"{config.jobstore_key_prefix}run_times"

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
            "ts": utc_stamp(),
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

    def wrap(self, fn: Callable, name: str | None = None) -> Callable:
        """Emit start / end / error records around each invocation of ``fn``.

        ``name`` overrides the recorded job name; pass the JOB_FUNCTIONS key
        so records, the lock key and the scheduler job id share one identity.
        Falling back to ``fn.__name__`` splits them the moment a registry key
        differs from its function's name — the dashboard builds its job list
        from registry keys, so those records land under a name the dropdown
        doesn't know and get labelled "(orphan)".
        """
        job = name or fn.__name__

        @wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            self.record(job, "start")
            t0 = time.monotonic()
            try:
                result = fn(*args, **kwargs)
            except Exception as exc:
                self.record(
                    job,
                    "error",
                    duration_ms=int((time.monotonic() - t0) * 1000),
                    error=repr(exc),
                )
                raise
            self.record(
                job,
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


def lock_owner_token() -> str:
    """Mint this acquisition's lock value: identity plus a uniqueness nonce.

    Handed to ``Lock.acquire(token=...)`` as the key's value. redis-py's
    release/extend scripts compare it byte-for-byte, so any unique string
    works — packing the holder's identity in means a contender that loses
    the race can report *who* beat it instead of a bare "lock held".
    """
    return json.dumps(
        {
            "token": uuid.uuid4().hex,
            "host": socket.gethostname(),
            "pid": os.getpid(),
            "acquired": utc_stamp(),
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
        # Both empty-string and None raise here (ValueError / TypeError), so
        # a missing or pre-upgrade bare-uuid value lands on owner = None.
        owner = json.loads(raw)
    except (ValueError, TypeError):
        owner = None
    if isinstance(owner, dict):
        info["holder"] = f"{owner.get('host')}:{owner.get('pid')}"
        info["held_since"] = owner.get("acquired")
    return info


def _renew_until_stopped(lock: Any, ttl: int, stop: threading.Event) -> None:
    """Re-arm ``lock``'s TTL every ``ttl // 3`` seconds until ``stop`` is set.

    This is what decouples ``ttl`` from job runtime. Without it, ``ttl`` has
    to be a bet on how long the task takes: bet low and the key expires
    mid-run so the next fire acquires cleanly and runs *concurrently* — the
    lock silently stops protecting; bet high and one hard kill orphans the
    key for the full ``ttl``. Renewing means a live run holds the lock for
    as long as it needs while ``ttl`` shrinks to just the orphan window.

    ``replace_ttl=True`` is required: ``extend`` otherwise *adds* to the
    remaining TTL, so every tick would push the expiry further out and a
    killed process would leave an orphan lasting far beyond ``ttl``.

    ``LockNotOwnedError`` means we lost ownership (the key expired and
    someone else took it). Stop immediately — the release in the wrapper's
    ``finally`` raises the same way, so we never delete the new owner's lock.
    """
    from redis.exceptions import LockNotOwnedError

    interval = max(ttl // 3, 1)
    while not stop.wait(interval):
        try:
            lock.extend(ttl, replace_ttl=True)
        except LockNotOwnedError:
            log.warning("lock %s no longer owned; stopping renewal", lock.name)
            return
        except Exception:
            # Transient Redis blips shouldn't kill the watchdog — the next
            # tick retries, and there is still ``ttl`` of runway left.
            log.exception("failed to renew lock %s", lock.name)


def _start_renewal(lock: Any, ttl: int) -> threading.Event:
    """Run :func:`_renew_until_stopped` on a daemon thread; return its stop flag.

    Daemon so a watchdog can never hold up interpreter shutdown — losing a
    renewal on exit is harmless, the key just expires on its own.
    """
    stop = threading.Event()
    threading.Thread(
        target=_renew_until_stopped,
        args=(lock, ttl, stop),
        name=f"lock-renew:{lock.name}",
        daemon=True,
    ).start()
    return stop


def redis_lock(
    client: Any,
    *,
    key: str,
    ttl: int,
    on_skip: Callable[[dict[str, Any]], None] | None = None,
) -> Callable[[Callable], Callable]:
    """Skip-if-held lock with owner-checked release and TTL renewal.

    Built on ``redis.lock.Lock``: its release/extend Lua scripts are the
    owner-checked compare-and-swap this needs, so we only DEL or re-EXPIRE
    the key while we still hold it. Each acquisition mints a fresh owner
    token (see :func:`lock_owner_token`) as the key's value. While the
    wrapped function runs, a daemon watchdog re-arms the TTL, so ``ttl``
    bounds only how long an orphaned lock outlives a killed process — not
    how long the job may take.

    Optional ``on_skip(holder_info)`` fires when another holder already has
    the lock; ``holder_info`` comes from :func:`describe_lock_holder`. It
    takes no job name — a generic lock has no business naming the caller's
    work, and the caller already knows which job it wrapped.
    """
    from redis.exceptions import LockError
    from redis.lock import Lock

    def decorator(fn: Callable) -> Callable:
        @wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            # A fresh Lock per call, and thread_local=False for two separate
            # reasons. Fresh: Lock stores the acquisition token on itself, so
            # a shared instance would let concurrent runs clobber each
            # other's. Not thread-local: the watchdog calls extend() from
            # another thread, and the default stashes the token in
            # threading.local() where that thread would find none and raise.
            lock = Lock(client, key, timeout=ttl, thread_local=False)
            if not lock.acquire(blocking=False, token=lock_owner_token()):
                if on_skip is not None:
                    on_skip(describe_lock_holder(client, key))
                return None
            stop = _start_renewal(lock, ttl)
            try:
                return fn(*args, **kwargs)
            finally:
                # Set first: the watchdog must not re-arm a key we are about
                # to delete, or a crash right after release would leave a
                # renewed orphan behind.
                stop.set()
                try:
                    lock.release()
                except LockError:
                    # Covers LockNotOwnedError — the key expired mid-run and
                    # someone else owns it now, so there is nothing of ours
                    # to delete. Swallow-and-log like every other Redis call
                    # here: observability never breaks the task.
                    log.exception("failed to release lock %s", key)

        return wrapper

    return decorator
