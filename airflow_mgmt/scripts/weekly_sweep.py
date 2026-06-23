"""Stateless date-keyed sweep: scan every row of a DataFrame at least once per
`period` when a job fires every `every`, with an `overlap` margin so rows that
drift a few positions between runs are never missed.

The motivating case: a 40000-50000 row ``pd.DataFrame`` that must have every row
checked at least once a week. 7 days / 1 hour = 168 hourly runs; one full sweep
of the frame is spread across those 168 runs, so each run handles only
~n/168 ≈ 270 rows instead of re-scanning the whole frame every hour.

Why "based on the date info":
  Each run owns a contiguous slice of the frame *sorted by its date column*, so
  the slot maps to a date BAND (oldest rows early in the week, newest rows late),
  not to a raw row index that could shift when the frame is re-pulled.

Why stateless:
  The slot is a pure function of the KST clock folded against a fixed epoch — no
  cursor to persist or corrupt. A missed run just defers that one band a cycle;
  the next sweep covers it. Pass the scheduler's logical/run datetime as ``now``
  (its ``logical_date``) so a delayed or retried run still scans its own band
  instead of jumping to the current wall-clock slot.

Overlap, and what it does NOT do:
  ``overlap`` re-covers rows that drift a few positions between runs (small
  insert/delete churn) so boundary rows stay covered. It does NOT cover bulk
  reindexing — if the frame's order changes wholesale between runs, size
  ``overlap`` from your real per-run churn (run this file to see the benchmark)
  or raise the coverage frequency.

This is the date/clock-keyed sibling of ``row_cycler.py`` (which slices by raw
row position with a fixed weekly cadence). Use this one when you want a single
general function parameterised by ``period`` / ``every`` and keyed on a date
column.

Run it for a usage demo + coverage proof over a synthetic 45000-row frame:

    python3 airflow_mgmt/scripts/weekly_sweep.py
"""

import random
from collections import Counter
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")

WEEK = timedelta(weeks=1)
HOUR = timedelta(hours=1)

# Fixed anchor so the slot is a pure function of the clock (no stored cursor).
# 2024-01-01 is a Monday 00:00 KST, so with the weekly/hourly defaults the slot
# equals the hour-of-week: a run at Mon 00:00 -> slot 0, Tue 00:00 -> slot 24, ...
_EPOCH = datetime(2024, 1, 1, tzinfo=KST)


def slot_count(period: timedelta = WEEK, every: timedelta = HOUR) -> int:
    """Runs that make one full sweep: e.g. 1 week / 1 hour = 168 slots."""
    slots = round(period / every)
    if slots < 1:
        raise ValueError(f"period ({period}) must be >= every ({every})")
    return slots


def current_slot(
    now: datetime | None = None,
    period: timedelta = WEEK,
    every: timedelta = HOUR,
) -> int:
    """Slot ``0 .. slot_count-1`` for ``now``, folded from the KST clock against
    a fixed epoch.

    Converts ``now`` to KST first, so a UTC-aware timestamp maps to the right KST
    interval. A naive ``now`` is taken to already be KST (per the ingest
    convention). Pass the scheduler's logical datetime so a delayed/retried run
    reading the same logical time lands in the same slot.
    """
    now = now or datetime.now(KST)
    now = now.replace(tzinfo=KST) if now.tzinfo is None else now.astimezone(KST)
    intervals = int((now - _EPOCH) // every)
    return intervals % slot_count(period, every)


def slot_bounds(slot: int, n: int, slots: int, overlap: int = 15) -> tuple[int, int]:
    """Half-open ``[lo, hi)`` row range this slot owns over ``n`` sorted rows.

    The fractional boundaries ``(slot * n) // slots`` tile ``[0, n)`` exactly for
    any ``n``, so the ``slots`` slots leave no gaps and adapt as ``n`` drifts.
    ``overlap`` rows of margin on each side re-cover boundary drift.
    """
    start = (slot * n) // slots
    end = ((slot + 1) * n) // slots
    return max(0, start - overlap), min(n, end + overlap)


def rows_for_run(
    df,
    sort_field,
    now: datetime | None = None,
    period: timedelta = WEEK,
    every: timedelta = HOUR,
    overlap: int = 15,
):
    """This run's slice of ``df`` to scan, so that over one ``period`` every row
    is visited at least once (twice inside the overlap bands).

    Args:
        df: a pandas DataFrame.
        sort_field: the date column (str) — or list of columns — to order by.
            This is the "date info" the slot maps onto. Include a unique
            tiebreaker (e.g. ``["timestamp", "id"]``) if timestamps repeat, so
            row order is reproducible run to run.
        now: the scheduler's logical/run datetime (KST). Defaults to ``now`` in
            KST; in production pass the scheduler's ``logical_date``.
        period: cover every row at least once per this span (default 1 week).
        every: how often the job fires (default 1 hour). ``period / every`` is
            the number of runs per full sweep.
        overlap: rows of margin re-covered at each slot boundary.

    Returns:
        An ``.iloc`` slice of ``df`` sorted by ``sort_field`` (treat as
        read-only — it is a copy under pandas Copy-on-Write).
    """
    ordered = df.sort_values(sort_field, kind="stable").reset_index(drop=True)
    slots = slot_count(period, every)
    lo, hi = slot_bounds(current_slot(now, period, every), len(ordered), slots, overlap)
    return ordered.iloc[lo:hi]


# ── runnable demo + coverage proof (pandas imported lazily under __main__) ────
def _demo_frame(n: int = 45000):
    """Synthetic frame standing in for the real one: a date column plus payload."""
    import pandas as pd

    base = datetime(2026, 1, 1, tzinfo=KST)
    # Spread the rows' own timestamps over ~120 days so sorting by date is
    # meaningful (oldest scanned early in the week, newest late).
    return pd.DataFrame(
        {
            "id": [f"R{i:06d}" for i in range(n)],
            "measured_at": [base + timedelta(minutes=4 * i) for i in range(n)],
            "value": [(i * 7) % 1000 for i in range(n)],
        }
    )


def _example_basic(df) -> None:
    """[1] The everyday call: this run's slice, then iterate and check each row."""
    print("\n[1] basic — this run's slice")
    todo = rows_for_run(df, "measured_at")  # weekly/hourly defaults, overlap=15
    slot = current_slot()
    print(
        f"  slot={slot}/{slot_count()}  rows={len(todo)}  "
        f"dates {todo.iloc[0].measured_at:%Y-%m-%d %H:%M} .. "
        f"{todo.iloc[-1].measured_at:%Y-%m-%d %H:%M}"
    )


def _example_walk_week(df) -> None:
    """[2] Walk all 168 hourly runs of a week and prove full coverage."""
    print("\n[2] one week (168 hourly runs) covers every row at least once")
    n = len(df)
    monday = datetime(2026, 6, 8, 0, 0, tzinfo=KST)  # any Monday 00:00 KST
    seen = Counter()
    sizes = []
    for h in range(slot_count()):  # 168 distinct hours == one full sweep
        sl = rows_for_run(df, "measured_at", now=monday + timedelta(hours=h))
        sizes.append(len(sl))
        seen.update(sl["id"].tolist())
    missed = n - len(seen)
    verdict = "all covered" if missed == 0 else f"MISSING {missed}"
    print(
        f"  rows={n}  per_run={min(sizes)}-{max(sizes)}  "
        f"unique_seen={len(seen)} — {verdict}  "
        f"(min_visits={min(seen.values())}, max_visits={max(seen.values())})"
    )


def _simulate_week(n: int, overlap: int, inserts: int, deletes: int, seed: int = 0) -> dict:
    """Walk 168 runs while the frame churns between runs; report rows missed.

    Each row has a stable id; its sort position is what drifts as rows are
    inserted/removed. Coverage is scored only against rows present the whole
    week (so eligible every run). ``overlap`` should drive ``missed`` to 0 for
    the churn rate you actually expect.
    """
    rng = random.Random(seed)
    ids = list(range(n))  # stable identities; list index == current sort position
    covered = Counter()
    next_id = n
    slots = slot_count()
    for run in range(slots):
        lo, hi = slot_bounds(run % slots, len(ids), slots, overlap)
        covered.update(ids[lo:hi])
        if run == slots - 1:
            break  # no churn after the last scheduled run
        for _ in range(deletes):
            if ids:
                del ids[rng.randrange(len(ids))]
        for _ in range(inserts):
            ids.insert(rng.randrange(len(ids) + 1), next_id)
            next_id += 1
    survivors = set(ids) & set(range(n))
    missed = sum(1 for i in survivors if covered[i] == 0)
    return {"overlap": overlap, "survivors": len(survivors), "missed": missed}


def _run_benchmark() -> None:
    """Size ``overlap`` against your expected per-run churn: pick the smallest
    overlap that zeroes ``missed`` for the (inserts, deletes) rate you expect."""
    print("\n[benchmark] n=45000 — rows missed over a week vs per-run churn:\n")
    for label, inserts, deletes in (("static", 0, 0), ("reshuffle", 20, 20), ("growth", 30, 10)):
        for overlap in (0, 15, 30, 60):
            s = _simulate_week(45000, overlap, inserts, deletes)
            print(f"  {label:<10} overlap={s['overlap']:>3}  missed={s['missed']:>4} / {s['survivors']}")
        print()


if __name__ == "__main__":
    print(f"weekly_sweep — {slot_count()} slots/week (hourly), epoch {_EPOCH:%Y-%m-%d %a}")
    frame = _demo_frame(45000)
    print(f"demo frame: {frame.shape[0]} rows x {frame.shape[1]} cols")
    _example_basic(frame)
    _example_walk_week(frame)
    _run_benchmark()
