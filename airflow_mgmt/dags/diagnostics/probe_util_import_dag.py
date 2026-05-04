"""
diagnostics / probe_util_import_dag.

Verifies the cross-platform sys.path bootstrap pattern: pick a per-host
root_dir (the parent of dags/) and insert it into sys.path so top-level
packages (utils, minio_handler, ...) resolve.

Hosts:
- Airflow worker  → /opt/airflow/dags/airflow_repo.git/skewnono-scheduler1
- Windows dev     → F:/skewnono
- Linux dev       → /project/workSpace  (also the fallback for any other OS)

Airflow is detected by checking whether the current working directory
contains "/opt/airflow" — the worker process runs under that root,
dev hosts (Windows / Linux at /project/workSpace) don't.
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
    dag_id="diagnostics_probe_util_import",
    description="Verify cross-platform sys.path bootstrap exposes top-level packages",
    start_date=datetime(2026, 1, 1),
    schedule=None,
    catchup=False,
    tags=["diagnostics"],
)
def probe_util_import():
    @task
    def probe() -> None:
        log.info("platform.system() = %r", system())
        log.info("Path.cwd()        = %s", Path.cwd())
        log.info("__file__          = %s", __file__)
        log.info("ROOT_DIR          = %s  (exists=%s)", ROOT_DIR, ROOT_DIR.exists())

        if ROOT_DIR.exists():
            log.info("Top-level entries in ROOT_DIR:")
            for child in sorted(ROOT_DIR.iterdir()):
                kind = "DIR " if child.is_dir() else "FILE"
                log.info("  %s %s", kind, child.name)

        log.info("sys.path:")
        for entry in sys.path:
            log.info("  %s", entry)

        log.info("Imports:")
        for name in ("utils", "utils.orders", "minio_handler"):
            log.info("  import %s", name)
            try:
                module = __import__(name, fromlist=["__file__"])
            except Exception:
                log.exception("    FAILED")
                continue
            log.info("    OK  __file__=%s", getattr(module, "__file__", "<none>"))

    probe()


probe_util_import()
