"""Periodic row cycler: cover every dataframe row at least `CYCLES_PER_WEEK`
times per week when a task runs hourly.

24 * 7 = 168 hourly runs per week. One full sweep of the dataframe takes
CYCLE_SLOTS runs; with CYCLES_PER_WEEK sweeps fitted into the week, each row
is visited that many times, evenly spaced. Per run a slot processes a
contiguous fraction of the dataframe — slot N owns rows
[(N*n)//CYCLE_SLOTS, ((N+1)*n)//CYCLE_SLOTS) — plus an `overlap` margin on
each side so rows that drift a few positions between runs stay covered at
slot boundaries.

    CYCLES_PER_WEEK = 1  -> CYCLE_SLOTS = 168, ~n/168 rows/run (once a week)
    CYCLES_PER_WEEK = 2  -> CYCLE_SLOTS =  84, ~n/84  rows/run (twice a week)
Pick a divisor of 168 (2, 3, 4, 6, 7, 8, ...) so every slot fires the same
number of times per week.

Stateless: the slot is derived from the wall clock (KST), so there is no
cursor to persist or corrupt. A missed run just defers that slice one cycle.

Caveat — drift vs. coverage: positional slicing assumes the dataframe stays
roughly the same order/length across the cycle. If rows are inserted/removed,
a row near a slot boundary can shift out of its slot before that slot fires
and be missed for that cycle. `overlap` re-covers small drift; it does NOT
cover bulk reindexing. The benchmark below measures exactly this — run it
with your expected per-run churn to size `overlap`.

Run it to see runnable usage examples (over a synthetic 15000-row frame)
followed by the coverage benchmark:

    python3 airflow_mgmt/scripts/row_cycler.py
"""

import random
from collections import Counter
from datetime import datetime, timedelta

from zoneinfo import ZoneInfo

RUNS_PER_WEEK = 24 * 7  # 168 hourly runs per week (fixed by the hourly schedule)
CYCLES_PER_WEEK = 2  # full sweeps per week; pick a divisor of RUNS_PER_WEEK
CYCLE_SLOTS = RUNS_PER_WEEK // CYCLES_PER_WEEK  # runs to complete one full sweep
KST = ZoneInfo("Asia/Seoul")


def current_slot(now: datetime | None = None) -> int:
    """Slot 0..CYCLE_SLOTS-1: position within the current sweep, from the KST
    hour-of-week folded into the cycle.

    Converts `now` to KST first, so a UTC-aware timestamp maps to the correct
    KST hour. A naive `now` is taken to already be KST (per the ingest
    convention), not machine-local.
    """
    now = now or datetime.now(KST)
    now = now.replace(tzinfo=KST) if now.tzinfo is None else now.astimezone(KST)
    return (now.weekday() * 24 + now.hour) % CYCLE_SLOTS


def slot_bounds(slot: int, n: int, overlap: int = 15) -> tuple[int, int]:
    """Half-open [lo, hi) row range this slot should process.

    Fractional boundaries `(slot * n) // CYCLE_SLOTS` tile [0, n) exactly
    for any n, so the CYCLE_SLOTS slots leave no gaps and adapt as n drifts.
    """
    start = (slot * n) // CYCLE_SLOTS
    end = ((slot + 1) * n) // CYCLE_SLOTS
    lo = max(0, start - overlap)
    hi = min(n, end + overlap)
    return lo, hi


def rows_for_this_run(df, overlap: int = 15, now: datetime | None = None):
    """This hour's slice of `df` (a pandas DataFrame), with `overlap` rows
    of margin on each side. Returns an `.iloc` slice (a copy under pandas
    Copy-on-Write — treat it as read-only).

    Sort `df` by a stable unique key before calling: the slot maps to row
    POSITIONS, so the order must be reproducible run to run or a row's slot
    shifts between runs.

    In production, pass `now` as the scheduler's logical/run datetime (e.g.
    the Airflow logical date) rather than relying on `datetime.now()`. The
    slot is derived from the clock, so a delayed or retried run reading the
    real wall clock can land in the next slot — skipping its own slice and
    double-covering the neighbour. The logical date keeps slot == intended
    hour regardless of when the run actually fires."""
    lo, hi = slot_bounds(current_slot(now), len(df), overlap)
    return df.iloc[lo:hi]


def _simulate_week(
    n: int,
    overlap: int,
    inserts: int = 0,
    deletes: int = 0,
    seed: int = 0,
) -> dict:
    """Walk all 168 hourly runs and report coverage of stable row identities.

    Each row carries a stable id; its list position is what drifts. Between
    runs — never after the final run — `deletes` random rows are removed and
    `inserts` fresh rows are added at random positions. Keeping
    `inserts != deletes` models net dataframe length drift, not just
    reshuffling.

    Coverage is scored against two honest denominators:
      - `orig`: original rows still present at week's end (so present every
        run), and
      - `new`: rows inserted mid-week and still present at the end.
    `min_visits` is the fewest times any whole-week survivor was checked — it
    should land near CYCLES_PER_WEEK once overlap covers the drift.
    """
    rng = random.Random(seed)
    ids = list(range(n))  # stable identities; list index == current position
    covered = Counter()
    next_id = n
    sizes = []

    for run in range(RUNS_PER_WEEK):
        lo, hi = slot_bounds(run % CYCLE_SLOTS, len(ids), overlap)
        sizes.append(hi - lo)
        covered.update(ids[lo:hi])
        if run == RUNS_PER_WEEK - 1:
            break  # no drift after the last scheduled run
        for _ in range(deletes):
            if ids:
                del ids[rng.randrange(len(ids))]
        for _ in range(inserts):
            ids.insert(rng.randrange(len(ids) + 1), next_id)
            next_id += 1

    present = set(ids)
    orig_survived = present & set(range(n))
    new_survived = present - set(range(n))  # ids >= n are mid-week inserts
    orig_visits = [covered[i] for i in orig_survived]
    new_visits = [covered[i] for i in new_survived]
    orig_missed = sum(1 for v in orig_visits if v == 0)
    new_missed = sum(1 for v in new_visits if v == 0)
    return {
        "rows": n,
        "overlap": overlap,
        "inserts": inserts,
        "deletes": deletes,
        "end_len": len(ids),
        "min_per_run": min(sizes),
        "max_per_run": max(sizes),
        "orig_survived": len(orig_survived),
        "orig_missed": orig_missed,
        "orig_missed_pct": 100 * orig_missed / len(orig_survived) if orig_survived else 0.0,
        "orig_min_visits": min(orig_visits) if orig_visits else 0,
        "new_survived": len(new_survived),
        "new_missed": new_missed,
        "new_missed_pct": 100 * new_missed / len(new_survived) if new_survived else 0.0,
    }


# ── runnable usage demos (only used under __main__; pandas imported lazily) ───
def _demo_frame(n: int = 15000):
    """A synthetic 2D dataframe standing in for your real one."""
    import pandas as pd

    return pd.DataFrame(
        {
            "eqp_id": [f"EQP{i:05d}" for i in range(n)],
            "recipe": [f"R{i % 37:02d}" for i in range(n)],
            "value": [(i * 7) % 1000 for i in range(n)],
        }
    )


def _demo_check(row) -> bool:
    """Stand-in for the real per-row check (FTP probe, OpenSearch lookup,
    recompute, ...). Return True on success; raise to signal a failure."""
    return row.value >= 0


def _example_basic(df) -> None:
    """[1] The everyday call: slice this hour, iterate, check each row."""
    print("\n[1] basic — this hour's slice")
    df = df.sort_values("eqp_id").reset_index(drop=True)  # stable positions
    todo = rows_for_this_run(df)  # = df.iloc[lo:hi], overlap=15 default
    ok = sum(_demo_check(r) for r in todo.itertuples(index=False))
    print(
        f"  slot={current_slot()}/{CYCLE_SLOTS}  rows={len(todo)}  ok={ok}  "
        f"ids {todo.iloc[0].eqp_id}..{todo.iloc[-1].eqp_id}"
    )


def _example_specific_hours(df) -> None:
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


def _example_full_cycle(df) -> None:
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


def _run_benchmark() -> None:
    """Sweep overlap across drift scenarios so you can size `overlap` to your
    expected per-run drift. `min_visits` should sit near CYCLES_PER_WEEK; pick
    the overlap that zeroes `orig_missed` for the (inserts, deletes) rate you
    actually expect."""
    print(f"\n[benchmark] n=15000  (orig = rows present the whole week):\n")
    scenarios = (
        ("static", 0, 0),
        ("reshuffle", 10, 10),
        ("net-growth", 15, 5),
        ("net-shrink", 5, 15),
    )
    for label, inserts, deletes in scenarios:
        for overlap in (0, 15, 30, 60):
            s = _simulate_week(15000, overlap, inserts=inserts, deletes=deletes)
            print(
                f"  {label:<10} overlap={s['overlap']:>3}  "
                f"end_len={s['end_len']:>5}  "
                f"per_run={s['min_per_run']}-{s['max_per_run']:<4}  "
                f"orig_missed={s['orig_missed']:>4} ({s['orig_missed_pct']:.2f}%)  "
                f"min_visits={s['orig_min_visits']}"
            )
        print()


if __name__ == "__main__":
    print(f"row_cycler — CYCLES_PER_WEEK={CYCLES_PER_WEEK}, CYCLE_SLOTS={CYCLE_SLOTS}")
    frame = _demo_frame(15000)
    print(f"demo frame: {frame.shape[0]} rows x {frame.shape[1]} cols")
    _example_basic(frame)
    _example_specific_hours(frame)
    _example_full_cycle(frame)
    _run_benchmark()
