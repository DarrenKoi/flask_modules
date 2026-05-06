"""
template / with_dag_template.

Boilerplate for a classic `with DAG(...)` workflow with explicit operators.

Use this when:
  - You prefer operator objects over TaskFlow decorators.
  - You want task creation, dependency wiring, and XCom pulls to be explicit.
  - You need to call repo-local helpers from scripts/, minio_handler/, or utils/.

Do NOT use this when:
  - A task needs packages NOT installed on the worker. Use
    virtualenv_task_template.py instead.
  - The workflow is mostly Python functions and simple return-value passing.
    taskflow_decorator_template.py is shorter for that case.

How to adapt:
  1. Copy this file into dags/<topic>/<name>_dag.py.
  2. Replace _list_targets(), _download_and_upload(), and _report() with your
     workflow callables.
  3. Keep the sys.path bootstrap above repo-local imports.

This file lives OUTSIDE airflow_mgmt/dags/ so Airflow does not auto-load
it. Copy into dags/<topic>/ and rename when you adapt it.
"""

import logging
import sys
from datetime import datetime
from pathlib import Path

from airflow.providers.standard.operators.python import PythonOperator
from airflow.sdk import DAG

log = logging.getLogger(__name__)


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

# Repo-local imports go AFTER the bootstrap. `scripts/` is now importable as
# a top-level package because airflow_mgmt/ is on sys.path.
from scripts.ftp_download_sample import collect_logs  # noqa: E402


def _list_targets() -> list[str]:
    # Real DAGs read this from a config file, Airflow Variable, or DB.
    return ["10.0.0.1", "10.0.0.2"]


def _download_and_upload(**context) -> dict:
    ti = context["ti"]
    ips = ti.xcom_pull(task_ids="list_targets")
    summary = collect_logs(ips)
    log.info("uploaded=%d failed=%d", summary["ok"], summary["ng"])
    return summary


def _report(**context) -> None:
    ti = context["ti"]
    summary = ti.xcom_pull(task_ids="download_and_upload")
    if summary["ng"]:
        log.warning("FTP failures: %s", summary["failed"])
    log.info("done: ok=%d ng=%d", summary["ok"], summary["ng"])


with DAG(
    dag_id="template_with_dag",
    description="Template: classic with-DAG workflow wrapping repo-local helpers",
    start_date=datetime(2026, 1, 1),
    schedule=None,
    catchup=False,
    tags=["template", "with-dag"],
) as dag:
    list_targets = PythonOperator(
        task_id="list_targets",
        python_callable=_list_targets,
    )
    download_and_upload = PythonOperator(
        task_id="download_and_upload",
        python_callable=_download_and_upload,
    )
    report = PythonOperator(
        task_id="report",
        python_callable=_report,
    )

    list_targets >> download_and_upload >> report
