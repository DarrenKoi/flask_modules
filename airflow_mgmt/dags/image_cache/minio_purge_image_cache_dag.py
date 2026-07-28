"""Delete SEM image-cache objects older than RETENTION_DAYS, nightly at 03:35 KST.

Thin scheduler wrapper. The actual purge logic lives in
scripts/minio_image_cache_purge.py so it can be tested from a plain
Python REPL without Airflow installed.

Dry-run is controlled by the Airflow Variable `image_cache_purge_dry_run`:
  - 'true' (default): log what would be deleted, change nothing
  - 'false':           perform the deletions

Flip the Variable in the UI (Admin -> Variables) once you trust the
logged candidates. No code change required.

Timezone: start_date is KST-aware, so the cron below is read as KST no matter
what `core.default_timezone` is set to. A naive start_date (what the other DAGs
in this repo use) would let a UTC-defaulted scheduler fire this at 12:35 KST.

Schedule vs retention: these are independent. The nightly run bounds how long a
7-day-old object can linger before collection to ~1 day; a weekly run would let
it reach ~14 days. Widen RETENTION_DAYS to change what "old" means; change the
cron to change how promptly old objects are collected.
"""

import logging
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

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
from scripts.minio_image_cache_purge import purge_modified_before  # noqa: E402


KST = ZoneInfo("Asia/Seoul")
BUCKET = "user"
# Relative to minio_config.PREFIX ("2067928/") — see the script's module
# docstring. Passing the full "2067928/image_cache/" here would double the
# namespace and silently match nothing.
PREFIX = "image_cache/"
RETENTION_DAYS = 7
DRY_RUN_VAR = "image_cache_purge_dry_run"


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
    result = purge_modified_before(
        storage,
        RETENTION_DAYS,
        prefix=PREFIX,
        dry_run=dry_run,
        logger=log,
    )

    log.info(
        "%s: %d objects older than %s (cutoff %s)",
        "DRY-RUN" if dry_run else "DELETED",
        result["candidate_count"],
        f"{RETENTION_DAYS}d",
        result["cutoff"],
    )
    if result["errors"]:
        raise RuntimeError(f"delete errors: {result['errors']}")
    return result


with DAG(
    dag_id="minio_purge_image_cache",
    description=f"Nightly 03:35 KST purge of image-cache objects older than {RETENTION_DAYS} days",
    start_date=datetime(2026, 1, 1, tzinfo=KST),
    schedule="35 3 * * *",
    catchup=False,
    tags=["image-cache", "maintenance"],
) as dag:
    PythonOperator(
        task_id="purge",
        python_callable=purge,
    )
