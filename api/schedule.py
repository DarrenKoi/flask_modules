"""Blueprint with dashboard, health, on-demand dispatch, log feed.

``init_jobs()`` composes ``redis_lock(task_logger.wrap(fn))`` so a skipped run
emits a single ``skip`` record (not ``start``/``skip``/``end``).
"""

import hmac
import json
from typing import Any, Callable

from apscheduler.events import EVENT_JOB_MAX_INSTANCES, EVENT_JOB_MISSED
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
from api.tasks.example_jobs import (
    daily_rollup,
    freshness_probe,
    halfhour_sync,
    hourly_extract,
    intraday_refresh,
    weekly_compaction,
)
from api.tasks.many_tasks import purge_old_logs, restart_uwsgi, task1, task2

bp = Blueprint("schedule", __name__)

SCHEDULE_TOKEN = ""
TOKEN_HEADER = "X-API-Token"

# ── Registry ────────────────────────────────────
#
# Four knobs matter, and for jobs that run minutes rather than milliseconds
# the last two matter most:
#
# `minute=`  Cron fires at an exact instant, so two jobs written `minute=0`
#            start *together*, not "around" the hour. The executor holds
#            max_workers=4 (extension.py), so the 5th concurrent job queues.
#            Give every job its own minute; see the staggering note below.
#            CronSlottingTests catches two jobs of *different periods*
#            landing on the same instant.
#
# `lock_ttl` Orphan-clear window only — a live run re-arms its own TTL, so it
#            may overrun this freely (see ApiRedisConfig.lock_ttl). Smaller is
#            better: it bounds how long a lock left by a killed process blocks
#            the job. Floor around 60s so the ttl//3 renewal keeps margin;
#            ceiling at one trigger interval, past which you just lose runs.
#            For daily/weekly jobs any value under the interval costs nothing,
#            so keep those small too.
#
# `misfire_grace_time`  How late a start may be and still happen. Checked when
#            the job reaches a worker thread, so it covers queue wait, not just
#            scheduler lag. The 60s default (extension.py) is too tight for
#            anything that can queue behind a 20-minute job: the run is dropped
#            with only a "was missed by" line in the uWSGI log and NO dashboard
#            record. Set it to how stale a start you would still accept.
#
# `executor` "fast" is a separate single-thread lane. Put short jobs there so
#            they cannot be starved for 20 minutes behind four long ones on
#            "default".
#
# Also: `manual_dispatch=False` removes the job from the /jobs/run_job allow
# list; the scheduler still fires it. Cron `hour=` is scheduler-local
# (Asia/Seoul). Omit `lock_ttl` (or pass None) to inherit the config default.
JOB_FUNCTIONS: dict[str, dict[str, Any]] = {
    "task1": {
        "fn": task1,
        # "4-59/5" (:04 :09 :14 …), not "*/5". A plain */5 lands on :00 :05
        # :10 … which hits every ten-minute slot below, so it would start
        # together with hourly_extract, halfhour_sync and intraday_refresh.
        # Phase-shifting keeps the cadence and clears the slots.
        "trigger": CronTrigger(minute="4-59/5"),
        "lock_ttl": 60,
        "manual_dispatch": True,
    },
    "task2": {
        "fn": task2,
        # Interval triggers fire relative to when the scheduler started, so
        # they cannot be slotted the way cron can — you cannot say "never at
        # :00". That alone is a reason to prefer CronTrigger for anything long
        # enough to contend for a thread.
        "trigger": IntervalTrigger(seconds=30),
        # Explicit, not the shared default: at a 30s interval every extra
        # minute of orphan TTL costs two more skipped runs.
        "lock_ttl": 60,
        "manual_dispatch": True,
    },
    # A daily job has to dodge the *hourly* slots too — an hourly job at
    # minute=0 also fires at 01:00, so "1 AM" and "hourly" collide unless the
    # daily ones take a free minute as well. :20 is clear of every recurring
    # entry below.
    "restart_uwsgi": {
        "fn": restart_uwsgi,
        "trigger": CronTrigger(hour=1, minute=20),
        "lock_ttl": 60,
        # Reload-the-service action: keep it on the scheduler's clock only.
        # Reachable manual dispatch would let any caller bounce the process.
        "manual_dispatch": False,
    },
    "purge_old_logs": {
        "fn": purge_old_logs,
        # An hour after restart_uwsgi, which rolls today's log file.
        "trigger": CronTrigger(hour=2, minute=20),
        "lock_ttl": 300,
        "manual_dispatch": True,
    },
    # ── Reference entries ───────────────────────
    # Mock bodies (api/tasks/example_jobs.py), real scheduling shapes. These
    # are the cases a fleet of 5-20 minute jobs runs into. Minutes are
    # hand-slotted on a ten-minute grid: :00 :10 :20 :30 :40 :50.
    "hourly_extract": {
        "fn": hourly_extract,
        # 15-20 min, hourly. Longest job takes the first slot so it owns a
        # thread before the shorter ones arrive.
        "trigger": CronTrigger(minute=0),
        # One interval. A crash costs exactly one run, never two.
        "lock_ttl": 3600,
        # Generous: worth running 15 min late, and it can queue behind peers.
        # Still under the interval, so a late run never collides with the next.
        "misfire_grace_time": 900,
        "manual_dispatch": False,
    },
    "halfhour_sync": {
        "fn": halfhour_sync,
        # 5-10 min, twice an hour. Deliberately NOT ":00,:30" — the top of the
        # hour is the most contended minute in any scheduler, so it sits in
        # its slot and half an hour later.
        "trigger": CronTrigger(minute="10,40"),
        "lock_ttl": 1800,
        "misfire_grace_time": 600,
        "manual_dispatch": False,
    },
    "freshness_probe": {
        "fn": freshness_probe,
        # Seconds long, every 5 min, phase-shifted off the :00/:05 grid so it
        # never lands on another job's slot. "2-59/5" -> :02 :07 :12 …
        "trigger": CronTrigger(minute="2-59/5"),
        "lock_ttl": 300,
        # Short on purpose: a freshness check that starts 5 min late is
        # answering a question nobody asked any more. Better dropped.
        "misfire_grace_time": 60,
        # Its own lane — on "default" it would sit behind up to four 20-minute
        # jobs and miss its window every time they overlap.
        "executor": "fast",
        "manual_dispatch": False,
    },
    "daily_rollup": {
        "fn": daily_rollup,
        # 20 min, nightly, off-peak and clear of restart_uwsgi (1 AM) and
        # purge_old_logs (2 AM).
        "trigger": CronTrigger(hour=3, minute=20),
        # NOT 86400. The next fire is a day away, so any TTL below that skips
        # zero runs — pick a small one and an orphan clears in minutes
        # instead of blocking tomorrow's run too.
        "lock_ttl": 600,
        # An hour late is still fine for a nightly aggregate; nothing else is
        # scheduled to contend at 3 AM anyway.
        "misfire_grace_time": 3600,
        "manual_dispatch": False,
    },
    "intraday_refresh": {
        "fn": intraday_refresh,
        # 5 min, but only when anyone is looking. Restricting the window is
        # free load shedding: 14 fires a day instead of 96.
        "trigger": CronTrigger(day_of_week="mon-fri", hour="8-19", minute=30),
        "lock_ttl": 3600,
        "misfire_grace_time": 600,
        "manual_dispatch": False,
    },
    "weekly_compaction": {
        "fn": weekly_compaction,
        # 20+ min, Sunday pre-dawn. Weekly work wants the emptiest hour it can
        # get, because a long job at a busy minute blocks a thread for peers.
        "trigger": CronTrigger(day_of_week="sun", hour=4, minute=50),
        # Same reasoning as daily_rollup: interval is a week, so keep the
        # orphan window short.
        "lock_ttl": 600,
        # Half a day late still beats waiting until next Sunday.
        "misfire_grace_time": 43200,
        "manual_dispatch": False,
    },
    # ── Trigger cookbook (commented — copy, rename, adjust) ──────
    # Not registered. Each entry is registry-shaped so it can be uncommented
    # as-is. Cron `hour=`/`minute=` are scheduler-local (Asia/Seoul).
    #
    # Every hour at :45 — one number means "that minute, every hour".
    # "mwf_report": {
    #     "fn": my_task,
    #     "trigger": CronTrigger(minute=45),
    # },
    #
    # Mon/Wed/Fri at 09:15 — comma list picks specific weekdays. Ranges work
    # too: "mon-fri" (weekdays), "sat,sun" (weekend).
    # "mwf_report": {
    #     "fn": my_task,
    #     "trigger": CronTrigger(day_of_week="mon,wed,fri", hour=9, minute=15),
    # },
    #
    # Weekdays, hourly at :45, office hours only — the fields AND together:
    # mon-fri AND 08-19h AND minute 45 -> 12 fires a day, 5 days a week.
    # "office_hours_pull": {
    #     "fn": my_task,
    #     "trigger": CronTrigger(day_of_week="mon-fri", hour="8-19", minute=45),
    # },
    #
    # Every 2 hours at :25 — "*/2" is a step over the hour field (00, 02, 04
    # …22). Pin the minute; leaving it out means "every minute of those hours".
    # "bihourly_sync": {
    #     "fn": my_task,
    #     "trigger": CronTrigger(hour="*/2", minute=25),
    # },
    #
    # Every 15 minutes, cron-style — "a-59/15" starts the step at :a, so
    # "5-59/15" -> :05 :20 :35 :50. Prefer this over IntervalTrigger for
    # anything long enough to contend for a thread: the fire minutes are
    # readable here and CronSlottingTests can check them against the registry.
    # "quarter_hour_poll": {
    #     "fn": my_task,
    #     "trigger": CronTrigger(minute="5-59/15"),
    # },
    #
    # Monthly — 1st at 06:35. `day="last"` gives the last day of the month;
    # `day="last fri"` the last Friday.
    # "monthly_archive": {
    #     "fn": my_task,
    #     "trigger": CronTrigger(day=1, hour=6, minute=35),
    # },
    #
    # Every 15 minutes, interval-style — fires relative to scheduler start,
    # so the wall-clock minutes drift with every deploy and cannot be slotted.
    # Fine for short, frequent, don't-care-when work; see task2's note.
    # "quarter_hour_poll": {
    #     "fn": my_task,
    #     "trigger": IntervalTrigger(minutes=15),
    # },
    #
    # Every 6 hours, interval-style — hours/minutes/seconds compose:
    # IntervalTrigger(hours=1, minutes=30) is every 90 minutes.
    # "six_hourly_sweep": {
    #     "fn": my_task,
    #     "trigger": IntervalTrigger(hours=6),
    # },
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


def record_missed_run(event: Any) -> None:
    """Log a ``missed`` record when APScheduler drops or refuses a fire.

    Covers the two failure modes that are otherwise invisible from the
    dashboard — their only trace is a line in the uWSGI log:

    - ``EVENT_JOB_MISSED``: the start was already older than
      ``misfire_grace_time`` when the job reached a worker thread (queue
      wait counts), so the run was dropped.
    - ``EVENT_JOB_MAX_INSTANCES``: the previous run of the same job was
      still executing, so this fire was refused.

    The missed event carries ``scheduled_run_time``; the max-instances one
    carries a ``scheduled_run_times`` list instead.
    ``TaskLogger.record`` needs no app context (it holds its own client),
    so none is pushed — this runs on the scheduler's dispatch thread.
    """
    app = scheduler.app
    if app is None:
        return
    if event.code == EVENT_JOB_MISSED:
        reason = "start missed by more than misfire_grace_time"
        scheduled = event.scheduled_run_time
    else:
        reason = "previous run still executing (max_instances)"
        scheduled = event.scheduled_run_times[0] if event.scheduled_run_times else None
    task_logger: TaskLogger = app.config["TASK_LOGGER"]
    task_logger.record(
        event.job_id,
        "missed",
        reason=reason,
        scheduled=scheduled.isoformat() if scheduled else None,
    )


def write_scheduler_heartbeat() -> None:
    """Refresh the scheduler-alive marker and next-run snapshot in Redis.

    Registered as a recurring job in the scheduler worker only. All workers
    read these keys from ``/health`` so request-only workers can answer
    "is the scheduler alive somewhere?" — instead of falsely reporting OK
    just because their own process is healthy. Both keys are written with a
    TTL > interval, so if the scheduler dies they expire naturally and
    every worker's ``/health`` flips to degraded with no extra plumbing.

    The next-run map piggybacks on this tick because only the scheduler
    worker can answer "when does each job fire next" — and it reads the
    *jobstore*, so the dashboard shows the schedule actually persisted in
    Redis, which can differ from JOB_FUNCTIONS until the next deploy boot.
    """
    app = scheduler.app
    if app is None:
        return
    with app.app_context():
        cfg: ApiRedisConfig = app.config["API_REDIS_CONFIG"]
        client = app.config["LOCK_CLIENT"]
        try:
            next_runs = {
                job.id: job.next_run_time.isoformat() if job.next_run_time else None
                for job in scheduler.get_jobs()
            }
            with client.pipeline() as pipe:
                pipe.set(cfg.heartbeat_key, utc_stamp(), ex=cfg.heartbeat_ttl)
                pipe.set(
                    cfg.next_runs_key, json.dumps(next_runs), ex=cfg.heartbeat_ttl
                )
                pipe.execute()
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
        # Dropped and refused fires otherwise leave NO dashboard record —
        # only a "was missed by" line in the uWSGI log (see the registry's
        # misfire_grace_time note). Scheduler worker only: idle schedulers
        # never fire, so registering there would just be dead weight.
        scheduler.add_listener(
            record_missed_run, EVENT_JOB_MISSED | EVENT_JOB_MAX_INSTANCES
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
    raw_runs: str | None = None
    next_runs: dict[str, Any] = {}
    if redis_status == "ok":
        try:
            heartbeat, raw_runs = lock_client.mget(cfg.heartbeat_key, cfg.next_runs_key)
        except Exception:
            heartbeat = None
        try:
            next_runs = json.loads(raw_runs) if raw_runs else {}
        except (ValueError, TypeError):
            # A corrupt snapshot must not degrade the heartbeat verdict.
            next_runs = {}

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
            "next_runs": next_runs,
        },
    )


@bp.get("/jobs/logs")
def jobs_logs() -> Any:
    cfg: ApiRedisConfig = current_app.config["API_REDIS_CONFIG"]
    lock_client = current_app.config["LOCK_CLIENT"]
    # No limit param means the whole retained list — the dashboard relies on
    # this so the cap is defined once, here, not mirrored in its JS.
    raw_limit = request.args.get("limit")
    limit = min(int(raw_limit), cfg.log_list_max) if raw_limit else cfg.log_list_max
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
