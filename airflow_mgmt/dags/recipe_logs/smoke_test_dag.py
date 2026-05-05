"""Single-IP end-to-end smoke test for recipe log collection.

Manually trigger from the Airflow UI. Tests FTP fetch -> MinIO upload ->
scratch cleanup with one IP so the failure mode of each layer is visible
without the noise of fan-out.

Before triggering:
  1. Set the SMOKE_IP constant below to an IP you know is reachable.
  2. Create the Airflow Connection FTP_CONN_ID (Admin -> Connections,
     type FTP, host can be empty since we override per-task).
  3. Verify MINIO_* env vars are set on the worker.

A successful run logs an "OK" line per uploaded file with its MinIO key
and byte size. A failed run raises so the task instance turns red.
"""

import logging
import os
import sys
from datetime import datetime
from pathlib import Path

from airflow.hooks.base import BaseHook
from airflow.sdk import dag, task

log = logging.getLogger(__name__)


# ── sys.path bootstrap ──────────────────────────────────────────────────────
ROOT_DIR = Path(os.getenv("AIRFLOW_MGMT_ROOT") or next(
    (str(p) for p in Path(__file__).resolve().parents if (p / "project_root.txt").is_file()),
    "",
)).resolve()
if not ROOT_DIR.is_dir():
    raise RuntimeError("Cannot find airflow_mgmt root. Set AIRFLOW_MGMT_ROOT.")
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))
# ────────────────────────────────────────────────────────────────────────────

from scripts.recipe_log_collector import collect_logs  # noqa: E402


# Edit this before triggering. Keep it as a constant (not a Variable) so
# the smoke test is intentionally hardcoded — you should be conscious of
# which IP you are pointing at when testing.
SMOKE_IP = "10.0.0.1"
FTP_CONN_ID = "recipe_ftp"


@dag(
    dag_id="recipe_logs_smoke_test",
    description="Single-IP end-to-end smoke test — manual trigger only",
    start_date=datetime(2026, 1, 1),
    schedule=None,
    catchup=False,
    tags=["recipe-logs", "smoke-test"],
)
def recipe_logs_smoke_test():

    @task
    def run_one() -> dict:
        conn = BaseHook.get_connection(FTP_CONN_ID)
        log.info("smoke-testing one IP: %s (port=%s)", SMOKE_IP, conn.port or 21)

        summary = collect_logs(
            [SMOKE_IP],
            user=conn.login,
            password=conn.password,
            port=conn.port or 21,
        )

        log.info("uploaded=%d failed=%d", summary["ok"], summary["ng"])
        for u in summary["uploaded"]:
            log.info("  OK   key=%s size=%d", u["key"], u["size"])
        for f in summary["failed"]:
            log.info("  FAIL %s", f)

        # If the only IP failed, raise so the task instance is red.
        # Mixed success/failure cannot happen with one IP, but the guard
        # is intentional: copy-pasting this DAG to a multi-IP version
        # should not silently swallow total failures.
        if summary["ng"] and not summary["ok"]:
            raise RuntimeError(f"smoke test: all uploads failed: {summary['failed']}")

        return summary

    run_one()


recipe_logs_smoke_test()
