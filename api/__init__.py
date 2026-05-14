"""Application factory for the api package.

Each uWSGI worker calls ``create_app()`` independently (because wsgi.ini
sets ``lazy-apps = true``), so every worker would naively get its own
scheduler thread firing against the same shared Redis job store.
APScheduler does not coordinate across schedulers that share a job store,
so we elect a single scheduler worker (``uwsgi.worker_id() == 1``) and let
the others serve requests only. The Redis distributed lock still backs
on-demand ``/jobs/run_job`` dispatch from any worker.
"""

import atexit

from flask import Flask

from api.extension import (
    ApiRedisConfig,
    TaskLogger,
    configure_scheduler,
    create_lock_client,
    scheduler,
)
from api.schedule import bp as schedule_bp
from api.schedule import init_jobs, reap_orphan_jobs

__all__ = [
    "ApiRedisConfig",
    "create_app",
    "scheduler",
]

_scheduler_atexit_registered = False


def _is_scheduler_worker() -> bool:
    """True iff this process should own the APScheduler thread.

    Under uWSGI: only worker 1. Otherwise (single-process ``flask run``,
    pytest): True.
    """
    try:
        import uwsgi  # type: ignore[import-not-found]
    except ImportError:
        return True
    return uwsgi.worker_id() == 1


def create_app(*, config: ApiRedisConfig | None = None) -> Flask:
    cfg = config or ApiRedisConfig()
    app = Flask(__name__)
    app.config["API_REDIS_CONFIG"] = cfg

    lock_client = create_lock_client(cfg)
    app.config["LOCK_CLIENT"] = lock_client
    app.config["TASK_LOGGER"] = TaskLogger(lock_client, cfg.log_list_key, cfg.log_list_max)

    is_scheduler_worker = _is_scheduler_worker()
    app.config["IS_SCHEDULER_WORKER"] = is_scheduler_worker

    configure_scheduler(app, cfg)
    scheduler.init_app(app)
    init_jobs(app, register_with_scheduler=is_scheduler_worker)
    if is_scheduler_worker:
        scheduler.start()
        reap_orphan_jobs()
        global _scheduler_atexit_registered
        if not _scheduler_atexit_registered:
            atexit.register(_shutdown_scheduler)
            _scheduler_atexit_registered = True

    app.register_blueprint(schedule_bp)
    return app


def _shutdown_scheduler() -> None:
    # Pause before shutdown to close the race that produces sporadic
    # "Error submitting job ... cannot schedule new futures after shutdown"
    # logs on worker recycle. pause() flips state to STATE_PAUSED so the
    # background trigger loop skips _process_jobs(); without it, a tick
    # mid-flight can call executor.submit_job() AFTER shutdown(wait=False)
    # has torn down the ThreadPoolExecutor.
    try:
        if scheduler.running:
            scheduler.pause()
            scheduler.shutdown(wait=False)
    except Exception:
        pass
