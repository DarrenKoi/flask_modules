"""
template / mixed_styles_template.

Demonstrates a classic with-DAG file that mixes PythonOperator with another
operator type (BashOperator). This is the explicit-operator version of a
small FTP ingest flow.

Two equivalent variants are defined in this file so you can see them
side by side:

  1. dag_id = "template_mixed_via_with_dag_a"
  2. dag_id = "template_mixed_via_with_dag_b"

Both produce the same task graph. Pick this style when you want explicit
operator objects, manual XCom pulls, or several non-Python operator types.

This file lives OUTSIDE airflow_mgmt/dags/ so Airflow does not auto-load
it. Copy into dags/<topic>/ and rename when you adapt it.
"""

import logging
import sys
from datetime import datetime
from pathlib import Path

from airflow.providers.standard.operators.bash import BashOperator
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
    return ["10.0.0.1", "10.0.0.2"]


def _download_and_upload(**ctx) -> dict:
    ti = ctx["ti"]
    ips = ti.xcom_pull(task_ids="list_targets")
    return collect_logs(ips)


def _list_targets_v2() -> list[str]:
    return ["10.0.0.1", "10.0.0.2"]


def _download_and_upload_v2(**ctx) -> dict:
    ti = ctx["ti"]
    ips = ti.xcom_pull(task_ids="list_targets")
    return collect_logs(ips)


# ---------------------------------------------------------------------------
# Variant 1: with DAG(...) context manager and explicit operators
# ---------------------------------------------------------------------------
with DAG(
    dag_id="template_mixed_via_with_dag_a",
    description="with DAG(...) containing PythonOperator and BashOperator",
    start_date=datetime(2026, 1, 1),
    schedule=None,
    catchup=False,
    tags=["template"],
) as dag_a:
    announce = BashOperator(
        task_id="announce_start",
        bash_command='echo "FTP ingest starting at $(date -Iseconds)"',
    )
    targets = PythonOperator(
        task_id="list_targets",
        python_callable=_list_targets,
    )
    summary = PythonOperator(
        task_id="download_and_upload",
        python_callable=_download_and_upload,
    )
    cleanup_marker = BashOperator(
        task_id="touch_done_marker",
        bash_command='touch /tmp/ftp_done_$(date +%Y%m%d).flag',
    )

    announce >> targets >> summary >> cleanup_marker


# ---------------------------------------------------------------------------
# Variant 2: with DAG(...) context manager, same graph
# ---------------------------------------------------------------------------
with DAG(
    dag_id="template_mixed_via_with_dag_b",
    description="with DAG(...) containing PythonOperator and BashOperator",
    start_date=datetime(2026, 1, 1),
    schedule=None,
    catchup=False,
    tags=["template"],
) as dag_b:
    announce = BashOperator(
        task_id="announce_start",
        bash_command='echo "FTP ingest starting at $(date -Iseconds)"',
    )
    targets_v2 = PythonOperator(
        task_id="list_targets",
        python_callable=_list_targets_v2,
    )
    summary_v2 = PythonOperator(
        task_id="download_and_upload",
        python_callable=_download_and_upload_v2,
    )
    cleanup_marker = BashOperator(
        task_id="touch_done_marker",
        bash_command='touch /tmp/ftp_done_$(date +%Y%m%d).flag',
    )

    announce >> targets_v2 >> summary_v2 >> cleanup_marker
