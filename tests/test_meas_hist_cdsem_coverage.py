"""Tests for the per-fab hourly gap detection in meas_hist_cdsem_coverage.

The OSSearch is mocked — no live cluster. Bucket ``key`` values are derived
from the same KST datetimes the grid is built from, so the fixture stays
correct by construction rather than relying on a hand-computed epoch.
"""

import unittest
from datetime import datetime, timezone
from unittest.mock import Mock

from api.tasks.meas_hist_cdsem_coverage import KST, find_missing_fab_hours


def _epoch_ms(dt: datetime) -> int:
    """Epoch UTC milliseconds for a KST-aware datetime (how OS reports ``key``)."""
    return int(dt.astimezone(timezone.utc).timestamp() * 1000)


def _hour(year, month, day, hour) -> datetime:
    return datetime(year, month, day, hour, tzinfo=KST)


class FindMissingFabHoursTests(unittest.TestCase):
    def _search_returning(self, buckets):
        search = Mock()
        search.aggregate.return_value = {"aggregations": {"hourly": {"buckets": buckets}}}
        return search

    def test_reports_partial_and_fully_empty_hours(self):
        now = _hour(2026, 6, 16, 4).replace(minute=30)
        h02, h03 = _hour(2026, 6, 16, 2), _hour(2026, 6, 16, 3)
        # 02:00 has only fabA; 03:00 has no bucket at all (fully empty).
        buckets = [
            {
                "key": _epoch_ms(h02),
                "fab_name": {"buckets": [{"key": "fabA", "doc_count": 5}]},
            }
        ]
        search = self._search_returning(buckets)

        out = find_missing_fab_hours(
            fabs=["fabA", "fabB"], hours=2, now=now, search=search
        )

        self.assertEqual(
            out,
            [
                {"fab_name": "fabB", "missing_hour": h02.isoformat()},
                {"fab_name": "fabA", "missing_hour": h03.isoformat()},
                {"fab_name": "fabB", "missing_hour": h03.isoformat()},
            ],
        )
        search.aggregate.assert_called_once()

    def test_aggregation_body_shape(self):
        now = _hour(2026, 6, 16, 4).replace(minute=30)
        search = self._search_returning([])

        find_missing_fab_hours(fabs=["fabA"], hours=3, now=now, search=search)

        aggs, kwargs = search.aggregate.call_args.args[0], search.aggregate.call_args.kwargs
        hist = aggs["hourly"]["date_histogram"]
        self.assertEqual(hist["calendar_interval"], "hour")
        self.assertEqual(hist["time_zone"], "Asia/Seoul")
        self.assertEqual(hist["min_doc_count"], 0)
        self.assertEqual(aggs["hourly"]["aggs"]["fab_name"]["terms"]["size"], 50)
        rng = kwargs["query"]["range"]["timestamp"]
        self.assertEqual(rng["gte"], "now-3h/h")
        self.assertEqual(rng["lt"], "now/h")

    def test_fully_covered_window_returns_empty(self):
        now = _hour(2026, 6, 16, 4).replace(minute=30)
        h02, h03 = _hour(2026, 6, 16, 2), _hour(2026, 6, 16, 3)
        both = {"buckets": [{"key": "fabA", "doc_count": 1}, {"key": "fabB", "doc_count": 1}]}
        buckets = [
            {"key": _epoch_ms(h02), "fab_name": both},
            {"key": _epoch_ms(h03), "fab_name": both},
        ]
        search = self._search_returning(buckets)

        out = find_missing_fab_hours(
            fabs=["fabA", "fabB"], hours=2, now=now, search=search
        )

        self.assertEqual(out, [])

    def test_empty_fab_list_returns_empty_without_querying(self):
        search = self._search_returning([])

        out = find_missing_fab_hours(fabs=[], hours=2, search=search)

        self.assertEqual(out, [])
        search.aggregate.assert_not_called()


if __name__ == "__main__":
    unittest.main()
