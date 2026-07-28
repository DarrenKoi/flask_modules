"""Delete MSR pickle partitions older than RETENTION_DAYS, nightly at 04:05 KST.

Thin scheduler wrapper. The purge logic lives in
scripts/hitachi_sem_partition_purge.py so it can be run from a plain Python
REPL without Airflow installed.

Layout (bucket / key):
    user / 2067928/hitachi_sem/{cdsem,hvsem}/{raw_msr,dict_pkl}/YYYY/MM/DD/...

Because the tree is date-partitioned, this sweeps whole ``YYYY/MM/DD``
partitions rather than testing every object's last_modified: the walk is one list
call per partition level instead of one per file, which matters when a single
day holds a large number of objects.

KINDS is ``("dict_pkl",)`` — deliberately NOT both. ``raw_msr`` is the raw
.MSR 원본; it has its own retention question and is not covered by the number
below. Widening this tuple deletes originals.

READ THIS BEFORE TURNING OFF DRY-RUN — this job deletes source data, not a
cache. skewnono only ever *reads* these pickles (msr_file's office adapter
calls MinioObject().get_pickle and never puts), and re-deriving one from the
raw .MSR text is explicitly out of scope for that app. Recovery means
re-running the upstream post-processing pipeline.

RETENTION_DAYS must stay clear of meas_hist's consumer window. skewnono serves
measurement history for 60 days and every meas_hist document holds the path to
its pickle — delete the object and the MSR detail view breaks for a search hit
the app still returns. 61 leaves one day of margin.

That margin is thinner than it looks. The 60-day window is anchored on
max(timestamp) across the meas_hist aliases, NOT on now. If ingestion lags L
days, the window reaches back to now-60-L, so the true margin is 1-L days and
any lag at all overruns it. Raise RETENTION_DAYS if ingestion is ever more than
a day behind.

Note the sibling one-off (scripts/hitachi_sem_partition_purge.py __main__)
defaults to 30 days across BOTH kinds. That is a reclaim tool, not this policy —
running it as-is would delete pickles the 60-day window still serves.

Dry-run is controlled by the Airflow Variable `msr_pickle_purge_dry_run`:
  - 'true' (default): log what would be deleted, change nothing
  - 'false':           perform the deletions
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
from airflow_mgmt.scripts.hitachi_sem_partition_purge import (  # noqa: E402
    BUCKET,
    purge_hitachi_sem,
)


KST = ZoneInfo("Asia/Seoul")
# Pickles only — raw_msr is the 원본 and is not covered by RETENTION_DAYS.
KINDS = ("dict_pkl",)
RETENTION_DAYS = 61
DRY_RUN_VAR = "msr_pickle_purge_dry_run"


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
    result = purge_hitachi_sem(
        storage,
        retention_days=RETENTION_DAYS,
        kinds=KINDS,
        dry_run=dry_run,
        logger=log,
    )

    log.info(
        "%s: %d partitions %s older than %dd (cutoff %s, today KST %s)",
        "DRY-RUN" if dry_run else "DELETED",
        result["deleted_count"],
        result["kinds"],
        RETENTION_DAYS,
        result["cutoff"],
        result["today"],
    )
    if result["errors"]:
        raise RuntimeError(f"delete errors: {result['errors']}")
    return result


with DAG(
    dag_id="minio_purge_old_pickles",
    description=f"Nightly 04:05 KST purge of MSR pickle partitions older than {RETENTION_DAYS} days",
    start_date=datetime(2026, 1, 1, tzinfo=KST),
    schedule="5 4 * * *",
    catchup=False,
    tags=["msr-pickle", "maintenance"],
) as dag:
    PythonOperator(
        task_id="purge",
        python_callable=purge,
    )
