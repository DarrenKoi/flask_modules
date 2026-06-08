"""
template / hourly_row_cycler_template.

Classic `with DAG(...)` hourly job that sweeps a large dataframe over a week:
each hourly run processes ONE contiguous slice so every row is checked at
least once per week. 24 * 7 = 168 runs/week, so ~n/168 rows per run (e.g.
15000 rows -> ~90/run, ~104-120/run with overlap=15).

The slot is derived from the wall clock (KST) at run time, so the DAG behaves
identically inside or outside Airflow. The only cost is the rare case where a
run is delayed or retried ACROSS an hour boundary: it then reads the later
hour and processes that slot's slice instead of its own. For "at least once a
week" that just defers ~n/168 rows to the following week — harmless here. If
you ever need the scheduled hour pinned regardless of execution time, swap
datetime.now(KST) for context["logical_date"] in _select_rows.

Use this when:
  - You have a fixed-ish set of rows to revisit on a weekly cadence and a
    task that runs hourly.
  - Per-row work is light enough that ~n/168 rows fit in one hourly run.

Do NOT use this when:
  - Newly inserted rows need to be checked within the same week. Positional
    slicing covers mid-week inserts only ~50% that week (they wait until the
    next week, worst case ~2 weeks from insert). Add a separate "new rows
    since last run" fast-path if that SLA matters. See row_cycler.py.
  - Per-row work is heavy I/O at ~n/168 rows/run that won't fit the hourly
    pod budget. Partition further with .expand() (see mapped_batch_template).

How to adapt:
  1. Copy this file into dags/<topic>/<name>_dag.py and rename dag_id.
  2. Implement _load_keys() (load your row identifiers in a STABLE order) and
    _check_one() (the actual per-row check).
  3. Uncomment the repo-local import you need (e.g. minio_handler) after the
    bootstrap; delete the bootstrap entirely if you end up importing nothing
    repo-local.
  4. Tune OVERLAP to your expected per-run row drift — benchmark it against
    your real churn with `python3 row_cycler.py` at the repo root.

This file lives OUTSIDE airflow_mgmt/dags/ so Airflow does not auto-load it.
The slot math mirrors the repo-root row_cycler.py, which carries the coverage
benchmark — keep them in sync if you change the boundary formula.
"""

import logging
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from airflow.providers.standard.operators.python import PythonOperator
from airflow.sdk import DAG

log = logging.getLogger(__name__)


# ── sys.path bootstrap ──────────────────────────────────────────────────────
# Walks parents of this file (or cwd when run from a REPL) for the marker
# file and puts airflow_mgmt/ on sys.path so repo-local packages
# (minio_handler/, scripts/, ...) import as top-level names.
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

# Repo-local imports go AFTER the bootstrap. Uncomment what you need to load
# your row keys, e.g.:
# from minio_handler import MinioObject  # noqa: E402


# ── weekly row-cycler math (mirrors repo-root row_cycler.py) ─────────────────
TOTAL_SLOTS = 24 * 7  # 168 hourly runs per week
KST = ZoneInfo("Asia/Seoul")
OVERLAP = 15  # rows of margin on each slice side; absorbs small per-run drift


def _slot_for(when: datetime) -> int:
    """Hour-of-week as 0..167 in KST. A naive datetime is taken to already be
    KST (ingest convention); an aware one is converted."""
    when = when.replace(tzinfo=KST) if when.tzinfo is None else when.astimezone(KST)
    return when.weekday() * 24 + when.hour


def _slot_bounds(slot: int, n: int, overlap: int = OVERLAP) -> tuple[int, int]:
    """Half-open [lo, hi) row range this slot owns. Fractional boundaries tile
    [0, n) exactly across the 168 slots, with `overlap` margin each side."""
    start = (slot * n) // TOTAL_SLOTS
    end = ((slot + 1) * n) // TOTAL_SLOTS
    return max(0, start - overlap), min(n, end + overlap)
# ────────────────────────────────────────────────────────────────────────────


def _load_keys() -> list[str]:
    """Return this dataframe's stable row identifiers in a STABLE order.

    Order matters: a row's slot is its position in this list, so the order
    must be reproducible run to run (sort by a key column, don't rely on
    incidental ordering). Returning ~15000 short ids is cheap; the slicing
    happens in _select_rows.

    Replace this with your real source, e.g.:
        df = MinioObject(...).get_dataframe("bucket", "keys.parquet")
        return df.sort_values("eqp_id")["eqp_id"].tolist()
    """
    raise NotImplementedError("load your row keys here")


def _check_one(key: str) -> None:
    """Run the per-row check for one identifier. Raise on failure.

    Replace with the real work (FTP probe, OpenSearch lookup, recompute, ...).
    """
    raise NotImplementedError("implement your per-row check here")


def _select_rows() -> list[str]:
    """Slice this hour's rows out of the full ordered key list."""
    keys = _load_keys()
    slot = _slot_for(datetime.now(KST))
    lo, hi = _slot_bounds(slot, len(keys))
    selected = keys[lo:hi]
    log.info(
        "slot=%d/%d slice=[%d:%d] rows=%d of %d",
        slot, TOTAL_SLOTS, lo, hi, len(selected), len(keys),
    )
    return selected


def _check_rows(**context) -> dict:
    """Check every row in this hour's slice, isolating per-row failures."""
    ti = context["ti"]
    keys = ti.xcom_pull(task_ids="select_rows")
    ok = 0
    failed = []
    for key in keys:
        try:
            _check_one(key)
            ok += 1
        except Exception as exc:  # one bad row must not sink the slice
            failed.append(key)
            log.warning("check failed key=%s err=%s", key, exc)
    return {"ok": ok, "ng": len(failed), "failed": failed}


def _report(**context) -> None:
    """Log the run summary; surface failures for alerting."""
    ti = context["ti"]
    summary = ti.xcom_pull(task_ids="check_rows")
    if summary["ng"]:
        log.warning("row checks failed: %d -> %s", summary["ng"], summary["failed"])
    log.info("done: ok=%d ng=%d", summary["ok"], summary["ng"])


with DAG(
    dag_id="template_hourly_row_cycler",
    description="Template: hourly weekly-cycle row checker (one slice/run, full sweep/week)",
    start_date=datetime(2026, 1, 1),
    schedule="0 * * * *",
    catchup=False,
    max_active_runs=1,
    tags=["template", "row-cycler", "hourly"],
) as dag:
    select_rows = PythonOperator(
        task_id="select_rows",
        python_callable=_select_rows,
    )
    check_rows = PythonOperator(
        task_id="check_rows",
        python_callable=_check_rows,
    )
    report = PythonOperator(
        task_id="report",
        python_callable=_report,
    )

    select_rows >> check_rows >> report
