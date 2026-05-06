"""
template / taskflow_decorator_template.

Default boilerplate for Python-heavy DAGs in this repo.

Use this when:
  - Your task logic can be expressed as normal Python functions.
  - The worker already has every required package installed.
  - You want task return values to flow to downstream tasks without manual
    xcom_pull() calls.
  - You need to call repo-local helpers from scripts/, minio_handler/, or utils/.

Do NOT use this when:
  - A task needs packages NOT installed on the worker. Use
    virtualenv_task_template.py instead.
  - The workflow is mostly non-Python operators. Keep those operators explicit
    and only wrap Python glue with @task.

How to adapt:
  1. Copy this file into dags/<topic>/<name>_dag.py.
  2. Replace list_targets(), download_and_upload(), and report() with your
     workflow tasks.
  3. Keep the sys.path bootstrap above repo-local imports.

This file lives OUTSIDE airflow_mgmt/dags/ so Airflow does not auto-load
it. Copy into dags/<topic>/ and rename when you adapt it.
"""

import logging
import sys
from datetime import datetime
from pathlib import Path

from airflow.providers.standard.operators.bash import BashOperator
from airflow.sdk import dag, task

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


@dag(
    dag_id="template_taskflow_decorator",
    description="Template: TaskFlow DAG wrapping repo-local helper code",
    start_date=datetime(2026, 1, 1),
    schedule=None,
    catchup=False,
    tags=["template", "taskflow"],
)
def template_taskflow_decorator() -> None:
    announce_start = BashOperator(
        task_id="announce_start",
        bash_command='echo "FTP ingest starting at $(date -Iseconds)"',
    )

    @task
    def list_targets() -> list[str]:
        # Real DAGs read this from a config file, Airflow Variable, or DB.
        return ["10.0.0.1", "10.0.0.2"]

    @task
    def download_and_upload(ips: list[str]) -> dict:
        summary = collect_logs(ips)
        log.info("uploaded=%d failed=%d", summary["ok"], summary["ng"])
        return summary

    @task
    def report(summary: dict) -> None:
        if summary["ng"]:
            log.warning("FTP failures: %s", summary["failed"])
        log.info("done: ok=%d ng=%d", summary["ok"], summary["ng"])

    targets = list_targets()
    summary = download_and_upload(targets)

    announce_start >> targets
    report(summary)


template_taskflow_decorator()
