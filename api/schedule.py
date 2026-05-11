"""Blueprint with dashboard, health, on-demand dispatch, log feed.

``init_jobs()`` composes ``redis_lock(task_logger.wrap(fn))`` so a skipped run
emits a single ``skip`` record (not ``start``/``skip``/``end``).
"""

import hashlib
import hmac
from datetime import datetime, timezone
from typing import Any, Callable

from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from flask import Blueprint, Flask, current_app, jsonify, render_template, request

from api.extension import (
    ApiRedisConfig,
    TaskLogger,
    read_task_logs,
    redis_lock,
    scheduler,
)
from api.tasks.many_tasks import purge_old_logs, restart_uwsgi, task1, task2

bp = Blueprint("schedule", __name__)

SCHEDULE_TOKEN = ""
TOKEN_HEADER = "X-API-Token"


def slot_minute(name: str, modulus: int = 60) -> int:
    """Stable minute-of-the-hour slot derived from the job name.

    Use for jobs where "any time within this hour is fine" — spreads N jobs
    across N slots without manual bookkeeping. MD5's first 4 bytes give a
    stable 32-bit int across interpreter restarts (unlike builtin ``hash()``,
    which is randomized per-process). Reserve manual ``minute=`` values for
    jobs whose ordering matters (e.g. purge after restart).
    """
    digest = hashlib.md5(name.encode()).digest()[:4]
    return int.from_bytes(digest, "big") % modulus


# `lock_ttl=None` falls back to ApiRedisConfig.lock_ttl (default 1200s).
# Cron triggers interpret `hour=` in scheduler timezone (Asia/Seoul).
# `manual_dispatch=False` removes the job from the /jobs/run_job allow list;
# the scheduler still fires it on the configured trigger via run_registered_job.
# For "any time within this hour is fine" jobs, use ``slot_minute(name)`` to
# spread across distinct minutes deterministically. The scheduler executor is
# capped at max_workers=4 (extension.py), so colliding fires are paced by the
# thread pool.
JOB_FUNCTIONS: dict[str, dict[str, Any]] = {
    "task1": {
        "fn": task1,
        "trigger": CronTrigger(minute="*/5"),
        "lock_ttl": 60,
        "manual_dispatch": True,
    },
    "task2": {
        "fn": task2,
        "trigger": IntervalTrigger(seconds=30),
        "lock_ttl": None,
        "manual_dispatch": True,
    },
    "restart_uwsgi": {
        "fn": restart_uwsgi,
        "trigger": CronTrigger(hour=1, minute=0),
        "lock_ttl": 60,
        # Reload-the-service action: keep it on the scheduler's clock only.
        # Reachable manual dispatch would let any caller bounce the process.
        "manual_dispatch": False,
    },
    "purge_old_logs": {
        "fn": purge_old_logs,
        # 2 AM — after restart_uwsgi (1 AM) has rolled today's log file.
        "trigger": CronTrigger(hour=2, minute=0),
        "lock_ttl": 300,
        "manual_dispatch": True,
    },
}


def _wrap(
    fn: Callable,
    *,
    lock_client: Any,
    task_logger: TaskLogger,
    key_prefix: str,
    ttl: int,
) -> Callable:
    logged = task_logger.wrap(fn)
    return redis_lock(
        lock_client,
        key=f"{key_prefix}{fn.__name__}",
        ttl=ttl,
        on_skip=lambda name: task_logger.record(name, "skip", message="lock held"),
    )(logged)


HEARTBEAT_JOB_ID = "_scheduler_heartbeat"


def run_registered_job(name: str) -> Any:
    """Top-level entry point APScheduler stores by import path, not by value.

    Two correctness concerns are addressed here:

    1. ``RedisJobStore`` pickles each job. Pickling a ``functools.wraps``-
       decorated closure walks the wrapper's ``__module__`` / ``__qualname__``
       — which point at the bare task — so a restored job would call
       ``task1`` directly, bypassing ``redis_lock`` + ``TaskLogger``.
       Storing this thin runner by name defers the lookup to fire time.

    2. ``flask_apscheduler`` does *not* push a Flask app context around job
       execution (the scheduler thread runs ``job.func(*args, **kwargs)``
       directly — see flask_apscheduler/scheduler.py). So we grab the bound
       app off the scheduler and push the context ourselves before touching
       ``app.config``.
    """
    app = scheduler.app
    if app is None:
        raise RuntimeError("scheduler has no bound Flask app; init_app not called")
    with app.app_context():
        wrapped: dict[str, Callable] = app.config.get("WRAPPED_JOBS", {})
        fn = wrapped.get(name)
        if fn is None:
            raise KeyError(f"unknown job: {name}")
        return fn()


def write_scheduler_heartbeat() -> None:
    """Refresh the scheduler-alive marker in Redis.

    Registered as a recurring job in the scheduler worker only. All workers
    read this key from ``/health`` so request-only workers can answer
    "is the scheduler alive somewhere?" — instead of falsely reporting OK
    just because their own process is healthy. The key is written with a
    TTL > interval, so if the scheduler dies the key expires naturally and
    every worker's ``/health`` flips to degraded with no extra plumbing.
    """
    app = scheduler.app
    if app is None:
        return
    with app.app_context():
        cfg: ApiRedisConfig = app.config["API_REDIS_CONFIG"]
        client = app.config["LOCK_CLIENT"]
        try:
            client.set(
                cfg.heartbeat_key,
                datetime.now(timezone.utc).isoformat(timespec="seconds"),
                ex=cfg.heartbeat_ttl,
            )
        except Exception:
            # Same swallow-and-log policy as TaskLogger: observability must
            # never crash the scheduler thread.
            current_app.logger.exception("failed to write scheduler heartbeat")


def init_jobs(app: Flask, *, register_with_scheduler: bool = True) -> None:
    """Wrap every JOB_FUNCTIONS entry; optionally register it on the scheduler.

    Call AFTER ``scheduler.init_app(app)`` and BEFORE ``scheduler.start()``.
    Pass ``register_with_scheduler=False`` on non-scheduler uWSGI workers —
    they still need ``WRAPPED_JOBS`` populated so on-demand dispatch via
    ``/jobs/run_job`` works, but they must not write jobs to the shared
    Redis job store.
    """
    cfg: ApiRedisConfig = app.config["API_REDIS_CONFIG"]
    lock_client = app.config["LOCK_CLIENT"]
    task_logger: TaskLogger = app.config["TASK_LOGGER"]

    wrapped: dict[str, Callable] = {}
    for name, spec in JOB_FUNCTIONS.items():
        ttl = spec["lock_ttl"] if spec["lock_ttl"] is not None else cfg.lock_ttl
        fn = _wrap(
            spec["fn"],
            lock_client=lock_client,
            task_logger=task_logger,
            key_prefix=cfg.lock_key_prefix,
            ttl=ttl,
        )
        wrapped[name] = fn
        if register_with_scheduler:
            scheduler.add_job(
                id=name,
                func="api.schedule:run_registered_job",
                args=[name],
                trigger=spec["trigger"],
                replace_existing=True,
            )
    app.config["WRAPPED_JOBS"] = wrapped

    if register_with_scheduler:
        scheduler.add_job(
            id=HEARTBEAT_JOB_ID,
            func="api.schedule:write_scheduler_heartbeat",
            trigger=IntervalTrigger(seconds=cfg.heartbeat_interval),
            replace_existing=True,
        )


@bp.get("/")
def dashboard() -> Any:
    return render_template("index.html", jobs=list(JOB_FUNCTIONS.keys()))


@bp.get("/health")
def health() -> Any:
    """Whole-service health, answerable from any worker.

    ``status`` is the answer a load balancer wants: "ok" iff this worker
    can reach Redis AND a scheduler heartbeat younger than ``heartbeat_ttl``
    exists in Redis. The heartbeat is written by the scheduler worker every
    ``heartbeat_interval`` seconds (see :func:`write_scheduler_heartbeat`),
    so request-only workers can answer accurately without trusting their
    own (idle) ``scheduler.running`` flag.

    ``scheduler.running`` / ``scheduler.jobs`` remain in the response as
    per-worker liveness — only meaningful when ``scheduler.role`` is
    ``scheduler``.
    """
    cfg: ApiRedisConfig = current_app.config["API_REDIS_CONFIG"]
    lock_client = current_app.config["LOCK_CLIENT"]
    redis_status = "ok"
    try:
        lock_client.ping()
    except Exception as exc:
        redis_status = f"error: {exc!r}"

    heartbeat: str | None = None
    if redis_status == "ok":
        try:
            heartbeat = lock_client.get(cfg.heartbeat_key)
        except Exception:
            heartbeat = None

    is_scheduler_worker = current_app.config.get("IS_SCHEDULER_WORKER", True)
    job_ids = [j.id for j in scheduler.get_jobs()] if scheduler.running else []
    overall = "ok" if redis_status == "ok" and heartbeat is not None else "degraded"
    return jsonify(
        status=overall,
        flask="ok",
        redis=redis_status,
        scheduler={
            "running": scheduler.running,
            "jobs": job_ids,
            "role": "scheduler" if is_scheduler_worker else "worker",
            "heartbeat": heartbeat,
        },
    )


@bp.get("/jobs/logs")
def jobs_logs() -> Any:
    cfg: ApiRedisConfig = current_app.config["API_REDIS_CONFIG"]
    lock_client = current_app.config["LOCK_CLIENT"]
    limit = min(int(request.args.get("limit", 100)), cfg.log_list_max)
    return jsonify(read_task_logs(lock_client, cfg.log_list_key, limit=limit))


@bp.post("/jobs/run_job")
def run_job() -> Any:
    # Fail-closed auth: empty SCHEDULE_TOKEN disables manual dispatch outright.
    # uWSGI binds 0.0.0.0:8000, so this endpoint is reachable by anyone who can
    # route to the service.
    if not SCHEDULE_TOKEN:
        return jsonify(error="manual dispatch disabled"), 403
    presented = request.headers.get(TOKEN_HEADER, "")
    if not hmac.compare_digest(presented, SCHEDULE_TOKEN):
        return jsonify(error="unauthorized"), 401

    payload = request.get_json(silent=True) or {}
    name = payload.get("job_name")
    spec = JOB_FUNCTIONS.get(name)
    if spec is None:
        return jsonify(error=f"unknown job: {name}"), 404
    if not spec.get("manual_dispatch", True):
        return jsonify(error=f"job {name!r} is not manually dispatchable"), 403

    wrapped: dict[str, Callable] = current_app.config.get("WRAPPED_JOBS", {})
    fn = wrapped.get(name)
    if fn is None:
        return jsonify(error=f"unknown job: {name}"), 404
    fn()
    return jsonify(status="ok", job=name)
