"""
template / mixed_styles_template.

Demonstrates that @task (TaskFlow) and classic operators (BashOperator,
PythonOperator, sensors) can live together inside a single DAG. The
DAG container chooses the syntax; the operators inside follow whatever
"current DAG" context is active.

Two equivalent variants are defined in this file so you can see them
side by side:

  1. dag_id = "template_mixed_via_at_dag"   — uses @dag + @task + BashOperator
  2. dag_id = "template_mixed_via_with_dag" — uses with DAG(...) + @task + BashOperator

Both produce the same task graph. Pick the style that matches the bulk
of your tasks. Rule of thumb:
  - mostly Python work → @dag (lower ceremony, nicer XCom)
  - mostly shell / sensor / pod work → with DAG(...) as dag

This file lives OUTSIDE airflow_mgmt/dags/ so Airflow does not auto-load
it. Copy into dags/<topic>/ and rename when you adapt it.
"""

import logging
import os
import sys
from datetime import datetime
from pathlib import Path

from airflow.providers.standard.operators.bash import BashOperator
from airflow.sdk import DAG, dag, task

log = logging.getLogger(__name__)


# ── sys.path bootstrap ──────────────────────────────────────────────────────
# Find airflow_mgmt/ on disk (the directory holding the project_root.txt
# marker file) and put it on sys.path so repo-local packages
# (scripts/, minio_handler/, ...) become importable as top-level names.
# Marker-file lookup is rename-safe — the parent folder can be called anything
# (repo/, dags_repo/, ...), only the marker inside has to be there.
# AIRFLOW_MGMT_ROOT env var overrides auto-detect — set it on Airflow workers
# if the parent walk can't find the marker file.
ROOT_DIR = Path(os.getenv("AIRFLOW_MGMT_ROOT") or next(
    (str(p) for p in Path(__file__).resolve().parents if (p / "project_root.txt").is_file()),
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


# ---------------------------------------------------------------------------
# Variant 1: @dag decorator with mixed task styles
# ---------------------------------------------------------------------------
@dag(
    dag_id="template_mixed_via_at_dag",
    description="@dag containing @task AND a classic BashOperator",
    start_date=datetime(2026, 1, 1),
    schedule=None,
    catchup=False,
    tags=["template"],
)
def template_mixed_via_at_dag():

    # Classic operator inside @dag — it auto-attaches because the
    # decorator established a "current DAG" context. No dag= argument.
    announce = BashOperator(
        task_id="announce_start",
        bash_command='echo "FTP ingest starting at $(date -Iseconds)"',
    )

    @task
    def list_targets() -> list[str]:
        return ["10.0.0.1", "10.0.0.2"]

    @task
    def download_and_upload(ips: list[str]) -> dict:
        return collect_logs(ips)

    cleanup_marker = BashOperator(
        task_id="touch_done_marker",
        bash_command='touch /tmp/ftp_done_$(date +%Y%m%d).flag',
    )

    # Bridging classic ↔ TaskFlow with `>>`. The @task return value
    # (an XComArg) chains to a classic operator the same way two
    # classic operators chain.
    targets = list_targets()
    summary = download_and_upload(targets)
    announce >> targets
    summary >> cleanup_marker


template_mixed_via_at_dag()


# ---------------------------------------------------------------------------
# Variant 2: with DAG(...) context manager, same mix of task styles
# ---------------------------------------------------------------------------
with DAG(
    dag_id="template_mixed_via_with_dag",
    description="with DAG(...) containing @task AND a classic BashOperator",
    start_date=datetime(2026, 1, 1),
    schedule=None,
    catchup=False,
    tags=["template"],
) as dag_obj:

    announce = BashOperator(
        task_id="announce_start",
        bash_command='echo "FTP ingest starting at $(date -Iseconds)"',
    )

    # @task ALSO works inside `with DAG(...)` — the decorator just
    # registers against whichever DAG context is active. This is the
    # "rule" in action: operators look up the current context, they
    # don't care which syntax created it.
    @task
    def list_targets_v2() -> list[str]:
        return ["10.0.0.1", "10.0.0.2"]

    @task
    def download_and_upload_v2(ips: list[str]) -> dict:
        return collect_logs(ips)

    cleanup_marker = BashOperator(
        task_id="touch_done_marker",
        bash_command='touch /tmp/ftp_done_$(date +%Y%m%d).flag',
    )

    targets_v2 = list_targets_v2()
    summary_v2 = download_and_upload_v2(targets_v2)
    announce >> targets_v2
    summary_v2 >> cleanup_marker
