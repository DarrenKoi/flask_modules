"""Runnable usage samples for scripts/row_cycler.

    python3 airflow_mgmt/scripts/examples.py

Builds a synthetic 15000-row 2D DataFrame and shows the three things you
actually do with the cycler:
  [1] pull this hour's slice and iterate its rows,
  [2] see how different hours map to different, non-overlapping slices,
  [3] confirm that one full sweep (CYCLE_SLOTS distinct hours) covers
      every row at least once.

The cycler slices by POSITION (iloc), so the dataframe must be in a stable
order across runs — sort by a unique key first. Every example does that.
"""

import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd


# ── sys.path bootstrap ──────────────────────────────────────────────────────
# Puts airflow_mgmt/ on sys.path so `scripts.row_cycler` imports as a
# top-level package whether this file is run directly or via `-m`.
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

from scripts.row_cycler import (  # noqa: E402
    CYCLE_SLOTS,
    CYCLES_PER_WEEK,
    current_slot,
    rows_for_this_run,
)

KST = ZoneInfo("Asia/Seoul")


def make_demo_frame(n: int = 15000) -> pd.DataFrame:
    """A synthetic 2D dataframe standing in for your real one."""
    return pd.DataFrame(
        {
            "eqp_id": [f"EQP{i:05d}" for i in range(n)],
            "recipe": [f"R{i % 37:02d}" for i in range(n)],
            "value": [(i * 7) % 1000 for i in range(n)],
        }
    )


def check_one(row) -> bool:
    """Stand-in for the real per-row check (FTP probe, OpenSearch lookup,
    recompute, ...). Return True on success; raise to signal a failure."""
    return row.value >= 0


def example_basic(df: pd.DataFrame) -> None:
    """[1] The everyday call: slice this hour, iterate, check each row."""
    print("\n[1] basic — this hour's slice")
    df = df.sort_values("eqp_id").reset_index(drop=True)  # stable positions
    todo = rows_for_this_run(df)  # = df.iloc[lo:hi], overlap=15 default
    ok = sum(check_one(r) for r in todo.itertuples(index=False))
    print(
        f"  slot={current_slot()}/{CYCLE_SLOTS}  rows={len(todo)}  ok={ok}  "
        f"ids {todo.iloc[0].eqp_id}..{todo.iloc[-1].eqp_id}"
    )


def example_specific_hours(df: pd.DataFrame) -> None:
    """[2] Pass an explicit `now` to see how the slice moves hour to hour."""
    print("\n[2] different hours -> different slices (overlap=0 to show the tiling)")
    df = df.sort_values("eqp_id").reset_index(drop=True)
    monday = datetime(2026, 6, 8, 0, 0, tzinfo=KST)  # a Monday 00:00 KST
    for h in (0, 1, 2, CYCLE_SLOTS):  # last one wraps to slot 0 again
        now = monday + timedelta(hours=h)
        sl = rows_for_this_run(df, overlap=0, now=now)
        print(
            f"  {now:%a %H:%M}  slot={current_slot(now):>2}  "
            f"rows[{sl.index[0]}:{sl.index[-1] + 1}] = {len(sl)}"
        )


def example_full_cycle(df: pd.DataFrame) -> None:
    """[3] Walk CYCLE_SLOTS consecutive hours and confirm full coverage."""
    print("\n[3] one full sweep covers every row")
    df = df.sort_values("eqp_id").reset_index(drop=True)
    n = len(df)
    monday = datetime(2026, 6, 8, 0, 0, tzinfo=KST)
    seen = set()
    for h in range(CYCLE_SLOTS):  # CYCLE_SLOTS distinct hours == one full sweep
        sl = rows_for_this_run(df, now=monday + timedelta(hours=h))
        seen.update(sl["eqp_id"].tolist())
    verdict = "all covered" if len(seen) == n else f"MISSING {n - len(seen)}"
    print(f"  after {CYCLE_SLOTS} hourly runs: {len(seen)}/{n} rows — {verdict}")


if __name__ == "__main__":
    print(
        f"row_cycler demo — CYCLES_PER_WEEK={CYCLES_PER_WEEK}, "
        f"CYCLE_SLOTS={CYCLE_SLOTS}"
    )
    frame = make_demo_frame(15000)
    print(f"built demo frame: {frame.shape[0]} rows x {frame.shape[1]} cols")
    example_basic(frame)
    example_specific_hours(frame)
    example_full_cycle(frame)
