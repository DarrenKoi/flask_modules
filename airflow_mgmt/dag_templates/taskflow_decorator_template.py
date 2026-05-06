"""
template / taskflow_decorator_template.

Boilerplate for a classic with-DAG PythonOperator workflow that calls helper
functions defined elsewhere in the repo — e.g., a long script in scripts/ or a
shared helper module added under airflow_mgmt/.

Use this when:
  - Your script's main() has been refactored into an importable function
    (collect_logs, run, process, ...).
  - All packages it needs are installed on the worker.
  - You prefer explicit operator objects and manual XCom wiring.

Do NOT use this when:
  - The helper needs packages NOT on the worker -> use PythonVirtualenvOperator
    (see virtualenv_task_template.py).
  - You're calling shell commands → use BashOperator instead.

How to refactor an existing main()-style script:
  1. Wrap main()'s body in a function with explicit args + return value.
  2. Keep `if __name__ == "__main__": run(...)` for local debugging.
  3. Import that function from the DAG (after the sys.path bootstrap).

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


def list_targets() -> list[str]:
    # Real DAGs read this from a config file, Airflow Variable, or DB.
    return ["10.0.0.1", "10.0.0.2"]


def download_and_upload(**ctx) -> dict:
    ti = ctx["ti"]
    ips = ti.xcom_pull(task_ids="list_targets")
    summary = collect_logs(ips)
    log.info("uploaded=%d failed=%d", summary["ok"], summary["ng"])
    return summary  # XCom return value must be JSON-serializable.


def report(**ctx) -> None:
    ti = ctx["ti"]
    summary = ti.xcom_pull(task_ids="download_and_upload")
    if summary["ng"]:
        log.warning("FTP failures: %s", summary["failed"])
    log.info("done: ok=%d ng=%d", summary["ok"], summary["ng"])


with DAG(
    dag_id="template_taskflow_decorator",
    description="Template: with-DAG PythonOperator wrapping an imported helper",
    start_date=datetime(2026, 1, 1),
    schedule=None,
    catchup=False,
    tags=["template"],
) as dag:
    list_targets_task = PythonOperator(
        task_id="list_targets",
        python_callable=list_targets,
    )
    download_and_upload_task = PythonOperator(
        task_id="download_and_upload",
        python_callable=download_and_upload,
    )
    report_task = PythonOperator(
        task_id="report",
        python_callable=report,
    )

    list_targets_task >> download_and_upload_task >> report_task
