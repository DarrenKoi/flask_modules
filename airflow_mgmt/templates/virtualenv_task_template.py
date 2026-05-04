"""
template / virtualenv_task_template.

Boilerplate for a task that needs packages NOT installed on the Airflow
worker. Airflow builds a fresh venv per run, pip-installs `requirements`,
runs the function in that subprocess, then tears the venv down.

Use this when:
  - You need a library the worker doesn't have (opensearch-py, redis, ...)
  - You want to pin a different version of a library than the worker has
  - You want strict isolation from worker globals

Do NOT use this when:
  - The function needs your repo's local packages (utils/, minio_handler/).
    Use a plain @task instead — those run in the worker process and can
    see the sys.path bootstrap.

This file lives OUTSIDE airflow_mgmt/dags/ so Airflow does not auto-load
it. Copy into dags/<topic>/ and rename when you adapt it.
"""

import logging
import sys
from datetime import datetime
from pathlib import Path
from platform import system

from airflow.sdk import dag, task

log = logging.getLogger(__name__)


def _root_dir() -> Path:
    if "/opt/airflow" in str(Path.cwd()):
        return Path("/opt/airflow/dags/airflow_repo.git/skewnono-scheduler1")
    if system() == "Windows":
        return Path("F:/skewnono")
    return Path("/project/workSpace")


ROOT_DIR = _root_dir()
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))


@dag(
    dag_id="template_virtualenv_task",
    description="Template: run a task in an isolated venv with extra pip packages",
    start_date=datetime(2026, 1, 1),
    schedule=None,
    catchup=False,
    tags=["template"],
)
def template_virtualenv_task():

    @task.virtualenv(
        requirements=[
            "opensearch-py==2.6.0",
            "redis==5.0.8",
        ],
        system_site_packages=False,
        # python_version="3.11",   # uncomment to pin; defaults to worker's python
        # venv_cache_path="/opt/airflow/venv_cache",  # speeds up reuse if set
    )
    def probe_os_and_redis(os_cfg: dict, redis_cfg: dict) -> dict:
        # ALL imports must be inside this function — they run in the venv,
        # not in the DAG-parsing process.
        import logging

        import redis
        from opensearchpy import OpenSearch

        log = logging.getLogger("airflow.task")

        os_client = OpenSearch(
            hosts=[{"host": os_cfg["host"], "port": os_cfg["port"]}],
            http_auth=(os_cfg["user"], os_cfg["password"]),
            use_ssl=os_cfg.get("use_ssl", False),
            verify_certs=os_cfg.get("verify_certs", False),
            ssl_show_warn=False,
            timeout=10,
        )
        os_info = os_client.info()
        log.info(
            "opensearch cluster=%s version=%s",
            os_info["cluster_name"],
            os_info["version"]["number"],
        )

        r = redis.Redis(
            host=redis_cfg["host"],
            port=redis_cfg["port"],
            db=redis_cfg.get("db", 0),
            password=redis_cfg.get("password"),
            decode_responses=True,
            socket_timeout=5,
        )
        pong = r.ping()
        log.info("redis ping=%s dbsize=%d", pong, r.dbsize())

        # XCom return — must be JSON-serializable.
        return {
            "opensearch": {
                "cluster_name": os_info["cluster_name"],
                "version": os_info["version"]["number"],
            },
            "redis": {"ping": pong},
        }

    # Hardcoded creds are fine in this repo (internal-only).
    os_cfg = {
        "host": "your-opensearch-host",
        "port": 9200,
        "user": "admin",
        "password": "REPLACE_ME",
        "use_ssl": True,
        "verify_certs": False,
    }
    redis_cfg = {
        "host": "your-redis-host",
        "port": 6379,
        "db": 0,
        "password": None,
    }

    probe_os_and_redis(os_cfg, redis_cfg)


template_virtualenv_task()
