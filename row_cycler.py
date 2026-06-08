"""Weekly row cycler: cover every dataframe row at least once per week
when a task runs hourly.

168 hourly slots per week (24 * 7). Each run takes a contiguous fraction
of the dataframe — slot N owns rows [(N*n)//168, ((N+1)*n)//168) — plus an
`overlap` margin on each side so rows that drift by a few positions between
runs stay covered at slot boundaries.

Stateless: the slot is derived from the wall clock (KST), so there is no
cursor to persist or corrupt. A missed run just defers that slice a week.

Caveat — drift vs. coverage: positional slicing assumes the dataframe stays
roughly the same order/length across the week. If rows are inserted/removed,
a row near a slot boundary can shift out of its slot before that slot fires
and be missed for the week. `overlap` re-covers small drift; it does NOT
cover bulk reindexing. The benchmark below measures exactly this — run it
with your expected per-run churn to size `overlap`.

    python3 row_cycler.py
"""

import random
from datetime import datetime

from zoneinfo import ZoneInfo

TOTAL_SLOTS = 24 * 7  # 168 hourly runs per week
KST = ZoneInfo("Asia/Seoul")


def current_slot(now: datetime | None = None) -> int:
    """Hour-of-week as 0..167 in KST (Mon 00:00 -> 0, Sun 23:00 -> 167).

    Converts `now` to KST first, so a UTC-aware timestamp (e.g. an Airflow
    logical date) maps to the correct KST hour-of-week. A naive `now` is
    taken to already be KST (per the ingest convention), not machine-local.
    """
    now = now or datetime.now(KST)
    now = now.replace(tzinfo=KST) if now.tzinfo is None else now.astimezone(KST)
    return now.weekday() * 24 + now.hour


def slot_bounds(slot: int, n: int, overlap: int = 15) -> tuple[int, int]:
    """Half-open [lo, hi) row range this slot should process.

    Fractional boundaries `(slot * n) // TOTAL_SLOTS` tile [0, n) exactly
    for any n, so the 168 slots leave no gaps and adapt as n drifts.
    """
    start = (slot * n) // TOTAL_SLOTS
    end = ((slot + 1) * n) // TOTAL_SLOTS
    lo = max(0, start - overlap)
    hi = min(n, end + overlap)
    return lo, hi


def rows_for_this_run(df, overlap: int = 15, now: datetime | None = None):
    """This hour's slice of `df` (a pandas DataFrame), with `overlap` rows
    of margin on each side. Returns an `.iloc` slice (a copy under pandas
    Copy-on-Write — treat it as read-only).

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
    """Walk all 168 slots and report coverage of stable row identities.

    Each row carries a stable id; its list position is what drifts. Between
    runs — never after the final slot, which has no run behind it — `deletes`
    random rows are removed and `inserts` fresh rows are added at random
    positions. Keeping `inserts != deletes` models net dataframe length
    drift, not just reshuffling.

    Coverage is scored against two honest denominators:
      - `orig`: original rows still present at week's end (so present every
        slot), and
      - `new`: rows inserted mid-week and still present at the end (eligible
        from the slot after they were inserted).
    Late-inserted rows that get only a slot or two before week's end and fall
    outside those slices show up as `new` misses — a real gap, surfaced here.
    """
    rng = random.Random(seed)
    ids = list(range(n))  # stable identities; list index == current position
    covered = set()
    next_id = n
    sizes = []

    for slot in range(TOTAL_SLOTS):
        lo, hi = slot_bounds(slot, len(ids), overlap)
        sizes.append(hi - lo)
        covered.update(ids[lo:hi])
        if slot == TOTAL_SLOTS - 1:
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
    orig_missed = len(orig_survived - covered)
    new_missed = len(new_survived - covered)
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
        "new_survived": len(new_survived),
        "new_missed": new_missed,
        "new_missed_pct": 100 * new_missed / len(new_survived) if new_survived else 0.0,
    }


if __name__ == "__main__":
    # Sweep overlap across drift scenarios so you can size `overlap` to your
    # expected per-run drift. `orig` = rows present the whole week; `new` =
    # rows inserted mid-week and surviving to the end. Pick the overlap that
    # zeroes both for the (inserts, deletes) rate you actually expect.
    print("Coverage by overlap (n=15000):\n")
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
                f"new_missed={s['new_missed']:>5} ({s['new_missed_pct']:.2f}%)"
            )
        print()
