"""Blueprint with dashboard, health, on-demand dispatch, log feed.

``init_jobs()`` composes ``redis_lock(task_logger.wrap(fn))`` so a skipped run
emits a single ``skip`` record (not ``start``/``skip``/``end``).
"""

import hmac
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
    utc_stamp,
)
from api.tasks.many_tasks import purge_old_logs, restart_uwsgi, task1, task2

bp = Blueprint("schedule", __name__)

SCHEDULE_TOKEN = ""
TOKEN_HEADER = "X-API-Token"

# Size `lock_ttl` at roughly one trigger interval: a crash then costs
# ceil(lock_ttl / interval) skipped runs and nothing more. A job may overrun
# it freely — see ApiRedisConfig.lock_ttl for why it is not a runtime budget.
# Omit the key (or pass None) to inherit that config default (300s).
# Cron triggers interpret `hour=` in scheduler timezone (Asia/Seoul).
# `manual_dispatch=False` removes the job from the /jobs/run_job allow list;
# the scheduler still fires it on the configured trigger via run_registered_job.
# Pick `minute=` values manually to spread overlapping jobs; the scheduler
# executor is capped at max_workers=4 (extension.py) so unavoidable collisions
# get paced by the thread pool.
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
        # Explicit, not the shared default: at a 30s interval every extra
        # minute of orphan TTL costs two more skipped runs.
        "lock_ttl": 60,
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


HEARTBEAT_JOB_ID = "_scheduler_heartbeat"


def _skip_recorder(task_logger: TaskLogger, job: str) -> Callable:
    """Build the ``on_skip`` callback that logs one ``skip`` record for ``job``.

    A factory, not a closure written inline in ``init_jobs``' loop: a closure
    would capture the loop *variable*, so by the time any job actually ran,
    every callback would report the last-registered name.
    """

    def record_skip(info: dict[str, Any]) -> None:
        task_logger.record(job, "skip", **info)

    return record_skip


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
            raise KeyError(
                f"unknown job: {name!r} — present in scheduler jobstore "
                f"but missing from JOB_FUNCTIONS. Likely an orphan from a "
                f"previous deploy; reap_orphan_jobs() clears it on next boot."
            )
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
            client.set(cfg.heartbeat_key, utc_stamp(), ex=cfg.heartbeat_ttl)
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
        # The registry key is the job's identity everywhere: lock key,
        # scheduler job id, WRAPPED_JOBS key and log records. Deriving any
        # of them from `fn.__name__` instead splits that identity as soon as
        # an entry is named differently from its function — and two entries
        # sharing one function would silently share one lock.
        wrapped[name] = redis_lock(
            lock_client,
            key=f"{cfg.lock_key_prefix}{name}",
            # `or` not `.get(key, default)`: an explicit `"lock_ttl": None`
            # must fall back too. Passing None through would reach
            # `SET ... ex=None`, i.e. a lock with NO expiry — one killed
            # process would then block the job forever.
            ttl=spec.get("lock_ttl") or cfg.lock_ttl,
            on_skip=_skip_recorder(task_logger, name),
        )(task_logger.wrap(spec["fn"], name))
        if register_with_scheduler:
            # Optional per-job scheduler settings.
            optional = {
                key: spec[key]
                for key in ("misfire_grace_time", "executor")
                if key in spec
            }
            scheduler.add_job(
                id=name,
                func="api.schedule:run_registered_job",
                args=[name],
                trigger=spec["trigger"],
                replace_existing=True,
                **optional,
            )
    app.config["WRAPPED_JOBS"] = wrapped

    if register_with_scheduler:
        scheduler.add_job(
            id=HEARTBEAT_JOB_ID,
            func="api.schedule:write_scheduler_heartbeat",
            trigger=IntervalTrigger(seconds=cfg.heartbeat_interval),
            replace_existing=True,
        )


def reap_orphan_jobs() -> None:
    """Remove jobs in the Redis jobstore that no longer exist in JOB_FUNCTIONS.

    ``RedisJobStore`` persists every registered job across restarts — that's
    why a scheduled job keeps firing after uWSGI bounces. The flip side is
    that removing a function from JOB_FUNCTIONS does NOT remove it from
    Redis. The old job entry continues firing on its trigger and
    ``run_registered_job`` raises ``KeyError`` because ``WRAPPED_JOBS`` is
    rebuilt from the current registry on every boot.

    Call once on the scheduler worker AFTER ``scheduler.start()`` — only
    then does ``get_jobs()`` read the merged in-memory + jobstore view.
    """
    keep = set(JOB_FUNCTIONS.keys()) | {HEARTBEAT_JOB_ID}
    for job in scheduler.get_jobs():
        if job.id not in keep:
            scheduler.remove_job(job.id)


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
    raw_age = request.args.get("max_age_seconds")
    max_age = int(raw_age) if raw_age else None
    return jsonify(
        read_task_logs(
            lock_client,
            cfg.log_list_key,
            limit=limit,
            max_age_seconds=max_age,
        )
    )


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
