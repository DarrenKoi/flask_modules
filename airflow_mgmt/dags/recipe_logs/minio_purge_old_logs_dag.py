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
import sys
from datetime import datetime
from pathlib import Path

from airflow.providers.standard.operators.python import PythonOperator
from airflow.sdk import DAG, Variable

log = logging.getLogger(__name__)


# ── sys.path bootstrap ──────────────────────────────────────────────────────
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
    raw = Variable.get(DRY_RUN_VAR, default="true").strip().lower()
    return raw not in {"false", "0", "no", "off"}


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


with DAG(
    dag_id="minio_purge_old_logs",
    description=f"Daily purge of date partitions older than {RETENTION_DAYS} days",
    start_date=datetime(2026, 1, 1),
    schedule="@daily",
    catchup=False,
    tags=["recipe-logs", "maintenance"],
) as dag:
    PythonOperator(
        task_id="purge",
        python_callable=purge,
    )
