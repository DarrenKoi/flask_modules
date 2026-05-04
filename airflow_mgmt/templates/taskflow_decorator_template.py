"""
template / taskflow_decorator_template.

Boilerplate for a TaskFlow DAG that calls helper functions defined
elsewhere in the repo — e.g., a long script in scripts/ or a shared
helper in utils/.

Use this when:
  - Your script's main() has been refactored into an importable function
    (collect_logs, run, process, ...).
  - All packages it needs are installed on the worker.
  - You want clean Python-style XCom passing between tasks.

Do NOT use this when:
  - The helper needs packages NOT on the worker → use @task.virtualenv
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
import os
import sys
from datetime import datetime
from pathlib import Path

from airflow.sdk import dag, task

log = logging.getLogger(__name__)


# ── sys.path bootstrap ──────────────────────────────────────────────────────
# Find airflow_mgmt/ on disk and put it on sys.path so repo-local packages
# (scripts/, minio_handler/, utils/, ...) become importable as top-level names.
# AIRFLOW_MGMT_ROOT env var overrides auto-detect — set it on Airflow workers
# if the parent walk can't find the airflow_mgmt directory.
ROOT_DIR = Path(os.getenv("AIRFLOW_MGMT_ROOT") or next(
    (str(p) for p in Path(__file__).resolve().parents if p.name == "airflow_mgmt"),
    "",
)).resolve()
if not ROOT_DIR.is_dir():
    raise RuntimeError("Cannot find airflow_mgmt root. Set AIRFLOW_MGMT_ROOT.")
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))
# ────────────────────────────────────────────────────────────────────────────

# Repo-local imports go AFTER the bootstrap. `scripts/` is now importable as
# a top-level package because airflow_mgmt/ is on sys.path.
from scripts.ftp_download_sample import collect_logs  # noqa: E402


@dag(
    dag_id="template_taskflow_decorator",
    description="Template: TaskFlow @task wrapping an imported helper",
    start_date=datetime(2026, 1, 1),
    schedule=None,
    catchup=False,
    tags=["template"],
)
def template_taskflow_decorator():

    @task
    def list_targets() -> list[str]:
        # Real DAGs read this from a config file, Airflow Variable, or DB.
        return ["10.0.0.1", "10.0.0.2"]

    @task
    def download_and_upload(ips: list[str]) -> dict:
        # The @task wrapper stays thin on purpose: orchestration lives
        # here, business logic stays in the importable module so it can
        # be unit-tested without Airflow.
        summary = collect_logs(ips)
        log.info("uploaded=%d failed=%d", summary["ok"], summary["ng"])
        return summary  # → XCom (must be JSON-serializable)

    @task
    def report(summary: dict) -> None:
        if summary["ng"]:
            log.warning("FTP failures: %s", summary["failed"])
        log.info("done: ok=%d ng=%d", summary["ok"], summary["ng"])

    # Plain Python composition — TaskFlow tracks the XCom dependencies.
    targets = list_targets()
    summary = download_and_upload(targets)
    report(summary)


template_taskflow_decorator()
