"""Delete MSR pickles older than RETENTION_DAYS, nightly at 04:05 KST.

Thin scheduler wrapper. The purge logic lives in scripts/minio_age_purge.py
so it can be tested from a plain Python REPL without Airflow installed.

READ THIS BEFORE TURNING OFF DRY-RUN — this job deletes source data, not a
cache. Unlike the image cache, nothing here re-creates a deleted object:
skewnono only ever *reads* these pickles (msr_file's office adapter calls
MinioObject().get_pickle and never puts), and re-deriving one from the raw
.MSR text is explicitly out of scope for that app. Recovery means re-running
the upstream post-processing pipeline.

Two things make the default safe and the live run risky:

1. RETENTION_DAYS must stay clear of meas_hist's consumer window. skewnono
   serves measurement history for 60 days, and every meas_hist document holds
   the path to its pickle — delete the object and the MSR detail view breaks
   for a search hit the app still returns. 61 leaves one day of margin.

   That margin is thinner than it looks. The 60-day window is anchored on
   max(timestamp) across the meas_hist aliases, NOT on now. If ingestion lags
   L days, the window reaches back to now-60-L, so the true margin is 1-L
   days and any lag at all overruns it. Raise RETENTION_DAYS if ingestion is
   ever more than a day behind.

2. PREFIX is not a retention unit. The raw .MSR original (minio_msr) and the
   post-processed pickle (minio_pkl) both live under hitachi_sem/, and the raw
   text is the true 원본. SUFFIX narrows the sweep to pickles — but its value
   below is a PLACEHOLDER, unverified against the real store. Leave dry-run on,
   read the logged object names, set SUFFIX to whatever actually distinguishes
   a pickle, and only then flip the Variable.

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
from scripts.minio_age_purge import purge_modified_before  # noqa: E402


KST = ZoneInfo("Asia/Seoul")
BUCKET = "user"
# Relative to minio_config.PREFIX ("2067928/") — passing the full
# "2067928/hitachi_sem/" would double the namespace and match nothing.
PREFIX = "hitachi_sem/"
# PLACEHOLDER — unverified against the office store. Confirm from a dry-run's
# logged object names before trusting it; a wrong suffix silently selects
# nothing (safe) or the raw .MSR originals (not safe).
SUFFIX = ".pkl"
# meas_hist serves 60 days; see the module docstring on why 61 is a thin margin.
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
    result = purge_modified_before(
        storage,
        RETENTION_DAYS,
        prefix=PREFIX,
        suffix=SUFFIX,
        dry_run=dry_run,
        logger=log,
    )

    log.info(
        "%s: %d objects (%s) older than %dd under %s (cutoff %s)",
        "DRY-RUN" if dry_run else "DELETED",
        result["candidate_count"],
        SUFFIX,
        RETENTION_DAYS,
        PREFIX,
        result["cutoff"],
    )
    if result["errors"]:
        raise RuntimeError(f"delete errors: {result['errors']}")
    return result


with DAG(
    dag_id="minio_purge_old_pickles",
    description=f"Nightly 04:05 KST purge of MSR pickles older than {RETENTION_DAYS} days",
    start_date=datetime(2026, 1, 1, tzinfo=KST),
    schedule="5 4 * * *",
    catchup=False,
    tags=["msr-pickle", "maintenance"],
) as dag:
    PythonOperator(
        task_id="purge",
        python_callable=purge,
    )
