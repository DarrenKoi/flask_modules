"""Application factory for the api package.

Each uWSGI worker calls ``create_app()`` independently (because uwsgi.ini
sets ``lazy-apps = true``), so every worker would naively get its own
scheduler thread firing against the same shared Redis job store.
APScheduler does not coordinate across schedulers that share a job store,
so we elect a single scheduler worker (``uwsgi.worker_id() == 1``) and let
the others serve requests only. The Redis distributed lock still backs
on-demand ``/jobs/run_job`` dispatch from any worker.
"""

import atexit
import os

from flask import Flask

from api.extension import (
    ApiRedisConfig,
    TaskLogger,
    configure_scheduler,
    create_lock_client,
    parse_bool,
    scheduler,
)
from api.schedule import TOKEN_ENV, bp as schedule_bp
from api.schedule import init_jobs

__all__ = [
    "ApiRedisConfig",
    "create_app",
    "scheduler",
]


def _is_scheduler_worker() -> bool:
    """True iff this process should own the APScheduler thread.

    Resolution order:
      1. ``API_SCHEDULER_ENABLED`` env var — explicit override, required for
         any multi-process non-uWSGI deployment (Flask reloader, gunicorn,
         honcho, etc.) where the implicit "I'm alone, therefore I'm leader"
         heuristic would let every child elect itself.
      2. Under uWSGI: only worker 1 owns the scheduler thread.
      3. Otherwise (single-process ``flask run``, pytest): True.
    """
    override = os.getenv("API_SCHEDULER_ENABLED")
    if override is not None:
        return parse_bool(override)
    try:
        import uwsgi  # type: ignore[import-not-found]
    except ImportError:
        return True
    return uwsgi.worker_id() == 1


def create_app(*, config: ApiRedisConfig | None = None) -> Flask:
    cfg = config or ApiRedisConfig.from_env()
    app = Flask(__name__)
    app.config["API_REDIS_CONFIG"] = cfg
    app.config[TOKEN_ENV] = os.getenv(TOKEN_ENV)

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
        atexit.register(_shutdown_scheduler)

    app.register_blueprint(schedule_bp)
    return app


def _shutdown_scheduler() -> None:
    try:
        if scheduler.running:
            scheduler.shutdown(wait=False)
    except Exception:
        pass
