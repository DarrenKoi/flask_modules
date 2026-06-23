"""Behavior tests for the stateless date-keyed weekly sweep."""

import unittest
from datetime import datetime, timedelta

import pandas as pd

from scripts.weekly_sweep import (
    KST,
    current_slot,
    rows_for_run,
    slot_bounds,
    slot_count,
)


def _frame(n: int) -> pd.DataFrame:
    base = datetime(2026, 1, 1, tzinfo=KST)
    return pd.DataFrame(
        {
            "id": [f"R{i:06d}" for i in range(n)],
            "measured_at": [base + timedelta(minutes=i) for i in range(n)],
        }
    )


class SlotCountTests(unittest.TestCase):
    def test_weekly_hourly_is_168_slots(self):
        self.assertEqual(slot_count(), 168)

    def test_period_scales_slot_count(self):
        self.assertEqual(slot_count(timedelta(days=3), timedelta(hours=6)), 12)

    def test_period_shorter_than_every_raises(self):
        with self.assertRaises(ValueError):
            slot_count(timedelta(hours=1), timedelta(days=1))


class CurrentSlotTests(unittest.TestCase):
    def test_monday_epoch_maps_to_hour_of_week(self):
        # 2026-06-08 is a Monday; with the Monday epoch, slot == hour-of-week.
        monday = datetime(2026, 6, 8, 0, 0, tzinfo=KST)
        self.assertEqual(current_slot(monday), 0)
        self.assertEqual(current_slot(monday + timedelta(hours=1)), 1)
        self.assertEqual(current_slot(monday + timedelta(days=1)), 24)
        # wraps after a full week
        self.assertEqual(current_slot(monday + timedelta(days=7)), 0)

    def test_utc_aware_now_is_converted_to_kst(self):
        # 15:00 UTC == 00:00 KST next day (+9h) -> slot 0 on the Monday.
        from zoneinfo import ZoneInfo

        sunday_utc = datetime(2026, 6, 7, 15, 0, tzinfo=ZoneInfo("UTC"))
        self.assertEqual(current_slot(sunday_utc), 0)

    def test_naive_now_treated_as_kst(self):
        naive = datetime(2026, 6, 8, 2, 0)  # Monday 02:00, assumed KST
        self.assertEqual(current_slot(naive), 2)


class SlotBoundsTests(unittest.TestCase):
    def test_tiles_with_no_gaps(self):
        n, slots = 1000, 168
        covered = set()
        for slot in range(slots):
            lo, hi = slot_bounds(slot, n, slots, overlap=0)
            covered.update(range(lo, hi))
        self.assertEqual(covered, set(range(n)))

    def test_overlap_extends_both_sides_and_clamps(self):
        lo, hi = slot_bounds(0, 1000, 168, overlap=15)
        self.assertEqual(lo, 0)  # clamped at the low edge
        plain_hi = (1 * 1000) // 168
        self.assertEqual(hi, plain_hi + 15)

    def test_last_slot_clamps_to_n(self):
        lo, hi = slot_bounds(167, 1000, 168, overlap=15)
        self.assertEqual(hi, 1000)


class RowsForRunTests(unittest.TestCase):
    def test_full_week_covers_every_row(self):
        df = _frame(45000)
        monday = datetime(2026, 6, 8, 0, 0, tzinfo=KST)
        seen = set()
        for h in range(slot_count()):
            sl = rows_for_run(df, "measured_at", now=monday + timedelta(hours=h))
            seen.update(sl["id"].tolist())
        self.assertEqual(len(seen), 45000)

    def test_slice_is_sorted_by_date_band(self):
        # Frame deliberately out of order; the run's slice must be date-ordered.
        df = _frame(2000).sample(frac=1, random_state=1).reset_index(drop=True)
        sl = rows_for_run(df, "measured_at", now=datetime(2026, 6, 8, 5, 0, tzinfo=KST))
        stamps = sl["measured_at"].tolist()
        self.assertEqual(stamps, sorted(stamps))

    def test_overlap_double_covers_boundary_rows(self):
        df = _frame(10000)
        monday = datetime(2026, 6, 8, 0, 0, tzinfo=KST)
        counts = {}
        for h in range(slot_count()):
            sl = rows_for_run(df, "measured_at", now=monday + timedelta(hours=h), overlap=15)
            for rid in sl["id"]:
                counts[rid] = counts.get(rid, 0) + 1
        # Every row seen at least once; boundary rows seen twice (overlap).
        self.assertGreaterEqual(min(counts.values()), 1)
        self.assertEqual(max(counts.values()), 2)

    def test_custom_period_every(self):
        # Every 6h over a 3-day window = 12 runs; still full coverage.
        df = _frame(5000)
        start = datetime(2026, 6, 8, 0, 0, tzinfo=KST)
        seen = set()
        for i in range(12):
            sl = rows_for_run(
                df,
                "measured_at",
                now=start + timedelta(hours=6 * i),
                period=timedelta(days=3),
                every=timedelta(hours=6),
            )
            seen.update(sl["id"].tolist())
        self.assertEqual(len(seen), 5000)


if __name__ == "__main__":
    unittest.main()
