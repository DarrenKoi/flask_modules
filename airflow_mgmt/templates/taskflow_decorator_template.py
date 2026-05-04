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

# Repo-local imports go AFTER the sys.path bootstrap.
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
