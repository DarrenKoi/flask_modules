"""Delete MinIO date partitions older than RETENTION_DAYS.

Thin scheduler wrapper. The actual purge logic lives in
scripts/minio_partition_purge.py so it can be tested from a plain
Python REPL without Airflow installed.

Dry-run is controlled by the Airflow Variable `minio_purge_dry_run`:
  - 'true' (default): log what would be deleted, change nothing
  - 'false':           perform the deletions

Flip the Variable in the UI (Admin -> Variables) once you trust the
logged candidates. No code change required.
"""

import logging
import os
import sys
from datetime import datetime
from pathlib import Path

from airflow.models import Variable
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

from minio_handler import MinioObject  # noqa: E402
from scripts.minio_partition_purge import purge_older_than  # noqa: E402


BUCKET = "eqp-logs"
RETENTION_DAYS = 90
DRY_RUN_VAR = "minio_purge_dry_run"


def _resolve_dry_run() -> bool:
    # Default to dry-run = True. Anything that does NOT explicitly say
    # "off" stays in dry-run mode — safe direction. The failure mode of
    # a misread Variable is "nothing got deleted", not "everything got
    # deleted".
    raw = Variable.get(DRY_RUN_VAR, default_var="true").strip().lower()
    return raw not in {"false", "0", "no", "off"}


@dag(
    dag_id="minio_purge_old_logs",
    description=f"Daily purge of date partitions older than {RETENTION_DAYS} days",
    start_date=datetime(2026, 1, 1),
    schedule="@daily",
    catchup=False,
    tags=["recipe-logs", "maintenance"],
)
def minio_purge_old_logs():

    @task
    def purge() -> dict:
        dry_run = _resolve_dry_run()
        storage = MinioObject(bucket=BUCKET)
        result = purge_older_than(storage, RETENTION_DAYS, dry_run=dry_run, logger=log)

        log.info(
            "%s: %d partitions older than %s",
            "DRY-RUN" if dry_run else "DELETED",
            result["candidate_count"],
            result["cutoff"],
        )
        if result["errors"]:
            raise RuntimeError(f"delete errors: {result['errors']}")
        return result

    purge()


minio_purge_old_logs()
