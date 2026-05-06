"""
template / python_operator_template.

Boilerplate for a classic PythonOperator DAG that calls helper
functions defined elsewhere in the repo. Use this if you prefer explicit
operator objects, or are working alongside other operator types
(BashOperator, EmailOperator, etc.).

PythonOperator is verbose but keeps task creation and XCom plumbing explicit.

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


def _list_targets(**ctx) -> list[str]:
    # PythonOperator passes Airflow's runtime context via **kwargs.
    # The function's return value lands in XCom under key "return_value"
    # automatically — no explicit xcom_push needed.
    return ["10.0.0.1", "10.0.0.2"]


def _download_and_upload(**ctx) -> dict:
    # With PythonOperator you pull XCom by hand.
    ti = ctx["ti"]
    ips = ti.xcom_pull(task_ids="list_targets")
    summary = collect_logs(ips)
    log.info("uploaded=%d failed=%d", summary["ok"], summary["ng"])
    return summary


def _report(**ctx) -> None:
    ti = ctx["ti"]
    summary = ti.xcom_pull(task_ids="download_and_upload")
    if summary["ng"]:
        log.warning("FTP failures: %s", summary["failed"])
    log.info("done: ok=%d ng=%d", summary["ok"], summary["ng"])


with DAG(
    dag_id="template_python_operator",
    description="Template: classic PythonOperator wrapping imported helpers",
    start_date=datetime(2026, 1, 1),
    schedule=None,
    catchup=False,
    tags=["template"],
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
