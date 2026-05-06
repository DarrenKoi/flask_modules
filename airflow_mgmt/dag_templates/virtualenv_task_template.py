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
  - The function needs your repo's local packages (minio_handler/, ...).
    Use a plain PythonOperator instead — those run in the worker process
    and can see the sys.path bootstrap.

This file lives OUTSIDE airflow_mgmt/dags/ so Airflow does not auto-load
it. Copy into dags/<topic>/ and rename when you adapt it.
"""

import logging
import os
import sys
from datetime import datetime
from pathlib import Path

from airflow.providers.standard.operators.python import PythonVirtualenvOperator
from airflow.sdk import DAG

log = logging.getLogger(__name__)


NEXUS_PYPI_INDEX_URL = "http://nexus.skhynix.com:8081/repository/pypi-group/simple"
NEXUS_PYPI_HOST = "nexus.skhynix.com"

# Airflow 3 uses uv when it is available. Set uv's default index before the
# operator creates its cached virtualenv, so seed packages also come from Nexus.
os.environ.setdefault("UV_DEFAULT_INDEX", NEXUS_PYPI_INDEX_URL)
os.environ.setdefault("UV_INDEX_URL", NEXUS_PYPI_INDEX_URL)
os.environ.setdefault("UV_INSECURE_HOST", NEXUS_PYPI_HOST)
os.environ.setdefault("PIP_INDEX_URL", NEXUS_PYPI_INDEX_URL)


# ── sys.path bootstrap ──────────────────────────────────────────────────────
# Walks parents of this file (or cwd when run from a REPL) for the marker
# file and puts that directory on sys.path so repo-local packages
# (scripts/, minio_handler/, ...) import as top-level names. Marker-file
# lookup is rename-safe — the parent folder can be called anything.
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
# ────────────────────────────────────────────────────────────────────────────


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
OS_CFG = {
    "host": "your-opensearch-host",
    "port": 9200,
    "user": "admin",
    "password": "REPLACE_ME",
    "use_ssl": True,
    "verify_certs": False,
}
REDIS_CFG = {
    "host": "your-redis-host",
    "port": 6379,
    "db": 0,
    "password": None,
}


with DAG(
    dag_id="template_virtualenv_task",
    description="Template: run a task in an isolated venv with extra pip packages",
    start_date=datetime(2026, 1, 1),
    schedule=None,
    catchup=False,
    tags=["template"],
) as dag:
    PythonVirtualenvOperator(
        task_id="probe_os_and_redis",
        python_callable=probe_os_and_redis,
        op_kwargs={"os_cfg": OS_CFG, "redis_cfg": REDIS_CFG},
        # Pin every version — the cache key hashes this list, so unpinned
        # entries can resolve to a new version on a later run and force a
        # cold rebuild. Keep this list short; if it grows past ~5 lines or
        # gets reused across DAGs, promote it to a module-level constant.
        requirements=[
            "opensearch-py==2.6.0",
            "redis==5.0.8",
        ],
        index_urls=[NEXUS_PYPI_INDEX_URL],
        system_site_packages=False,
        # Cache key = hash(requirements + python_version + system_site_packages).
        # Pinned versions in the requirements file mean repeated runs hit the
        # same cached venv (cold start ~30s → warm ~1s). Path is shared across
        # all DAGs on the worker — that's intentional, identical requirements
        # = same venv.
        venv_cache_path="/opt/airflow/venv_cache",
        # python_version="3.11",   # uncomment to pin; defaults to worker's python
    )
