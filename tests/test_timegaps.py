import unittest
from zoneinfo import ZoneInfo

import pandas as pd

from utils.timegaps import collapse_gaps, find_hourly_gaps

KST = ZoneInfo("Asia/Seoul")


def _ts(*hours):
    """Build a tz-aware KST timestamp Series for the given hour-of-day ints."""
    return pd.Series(
        [pd.Timestamp(2026, 6, 4, h, tz=KST) for h in hours]
    )


class FindHourlyGapsTests(unittest.TestCase):
    def test_single_gap_in_one_fab(self):
        # Hours 0,1,3 present -> hour 2 is the lone gap.
        df = pd.DataFrame(
            {"timestamp": _ts(0, 1, 3), "fab_name": "M1"}
        )
        gaps = find_hourly_gaps(df)

        self.assertEqual(list(gaps), ["M1"])
        missing = gaps["M1"]
        self.assertEqual(list(missing.columns), ["fab_name", "missing_hour"])
        self.assertEqual(len(missing), 1)
        self.assertEqual(
            missing["missing_hour"].iloc[0], pd.Timestamp(2026, 6, 4, 2, tz=KST)
        )

    def test_output_preserves_tzaware_dtype(self):
        df = pd.DataFrame({"timestamp": _ts(0, 2), "fab_name": "M1"})
        missing = find_hourly_gaps(df)["M1"]["missing_hour"]
        # Same tz-aware datetime dtype as the input (resolution-agnostic).
        self.assertEqual(missing.dtype, df["timestamp"].dtype)
        self.assertEqual(str(missing.dt.tz), "Asia/Seoul")

    def test_fab_with_no_gaps_is_omitted(self):
        df = pd.DataFrame({"timestamp": _ts(0, 1, 2), "fab_name": "M1"})
        self.assertEqual(find_hourly_gaps(df), {})

    def test_single_row_fab_has_no_gaps(self):
        # min == max -> grid is one hour -> nothing missing.
        df = pd.DataFrame({"timestamp": _ts(5), "fab_name": "M1"})
        self.assertEqual(find_hourly_gaps(df), {})

    def test_multiple_fabs_checked_independently(self):
        df = pd.DataFrame(
            {
                "timestamp": pd.concat([_ts(0, 2), _ts(0, 1, 2)], ignore_index=True),
                "fab_name": ["M1", "M1", "M2", "M2", "M2"],
            }
        )
        gaps = find_hourly_gaps(df)
        # M1 has a gap at hour 1; M2 is fully covered.
        self.assertEqual(list(gaps), ["M1"])
        self.assertEqual(
            gaps["M1"]["missing_hour"].iloc[0], pd.Timestamp(2026, 6, 4, 1, tz=KST)
        )

    def test_nat_timestamps_are_dropped(self):
        valid = _ts(0, 2)
        # Keep the NaT row the same tz-aware datetime dtype so the column
        # stays datetime-like rather than collapsing to object.
        with_nat = pd.concat(
            [valid, pd.Series([pd.NaT], dtype=valid.dtype)], ignore_index=True
        )
        df = pd.DataFrame({"timestamp": with_nat, "fab_name": "M1"})
        gaps = find_hourly_gaps(df)
        # NaT row dropped; span is 0..2 -> single gap at hour 1.
        self.assertEqual(len(gaps["M1"]), 1)

    def test_custom_column_names(self):
        df = pd.DataFrame({"ts": _ts(0, 2), "tool": "T9"})
        gaps = find_hourly_gaps(df, time_col="ts", group_col="tool")
        self.assertEqual(list(gaps["T9"].columns), ["tool", "missing_hour"])
        self.assertEqual(gaps["T9"]["tool"].iloc[0], "T9")


class CollapseGapsTests(unittest.TestCase):
    def test_consecutive_hours_fold_into_one_range(self):
        # Hours 0 and 5 present -> 1,2,3,4 missing as one 4-hour run.
        df = pd.DataFrame({"timestamp": _ts(0, 5), "fab_name": "M1"})
        collapsed = collapse_gaps(find_hourly_gaps(df)["M1"])

        self.assertEqual(len(collapsed), 1)
        row = collapsed.iloc[0]
        self.assertEqual(row["gap_start"], pd.Timestamp(2026, 6, 4, 1, tz=KST))
        self.assertEqual(row["gap_end"], pd.Timestamp(2026, 6, 4, 4, tz=KST))
        self.assertEqual(row["n_hours"], 4)

    def test_separate_runs_stay_separate(self):
        # Present 0,2,4 -> missing 1 and 3 as two distinct single-hour runs.
        df = pd.DataFrame({"timestamp": _ts(0, 2, 4), "fab_name": "M1"})
        collapsed = collapse_gaps(find_hourly_gaps(df)["M1"])

        self.assertEqual(len(collapsed), 2)
        self.assertEqual(list(collapsed["n_hours"]), [1, 1])
        self.assertEqual(
            list(collapsed["gap_start"]),
            [pd.Timestamp(2026, 6, 4, 1, tz=KST), pd.Timestamp(2026, 6, 4, 3, tz=KST)],
        )

    def test_empty_input_returns_empty_frame(self):
        empty = pd.DataFrame({"fab_name": [], "missing_hour": []})
        collapsed = collapse_gaps(empty)
        self.assertEqual(len(collapsed), 0)
        self.assertEqual(
            list(collapsed.columns), ["gap_start", "gap_end", "n_hours"]
        )


if __name__ == "__main__":
    unittest.main()
