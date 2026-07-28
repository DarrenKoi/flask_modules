"""Behavior tests for the last_modified-based age purge."""

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

from scripts.minio_age_purge import (
    SAMPLE_SIZE,
    iter_expired,
    purge_modified_before,
)

NOW = datetime(2026, 7, 28, 3, 35, tzinfo=timezone.utc)
PREFIX = "image_cache/"


class _Obj:
    def __init__(self, object_name, last_modified):
        self.object_name = object_name
        self.last_modified = last_modified


def _storage(*objects) -> Mock:
    """MinioObject stand-in — list() yields full object_names, as MinIO does."""
    storage = Mock()
    storage.list.return_value = iter(objects)
    storage.delete_many.return_value = []
    return storage


def _aged(name: str, days: float) -> _Obj:
    return _Obj(f"2067928/image_cache/{name}", NOW - timedelta(days=days))


class IterExpiredTests(unittest.TestCase):
    def test_selects_only_objects_older_than_cutoff(self):
        storage = _storage(_aged("a.jpeg", 8), _aged("b.jpeg", 6))
        found = list(iter_expired(storage, NOW - timedelta(days=7), PREFIX))
        self.assertEqual([n for n, _ in found], ["2067928/image_cache/a.jpeg"])

    def test_lists_recursively_under_the_given_prefix(self):
        storage = _storage()
        list(iter_expired(storage, NOW, PREFIX))
        storage.list.assert_called_once_with(prefix=PREFIX, recursive=True)

    def test_skips_entries_without_last_modified(self):
        """S3 common prefixes carry last_modified=None; never rank or delete them."""
        storage = _storage(_Obj("2067928/image_cache/dir/", None), _aged("a.jpeg", 9))
        found = list(iter_expired(storage, NOW - timedelta(days=7), PREFIX))
        self.assertEqual([n for n, _ in found], ["2067928/image_cache/a.jpeg"])

    def test_naive_last_modified_is_read_as_utc(self):
        storage = _storage(_Obj("2067928/image_cache/a.jpeg", datetime(2026, 7, 1)))
        found = list(iter_expired(storage, NOW - timedelta(days=7), PREFIX))
        self.assertEqual(len(found), 1)


class PurgeModifiedBeforeTests(unittest.TestCase):
    def test_deletes_expired_objects_by_full_object_name(self):
        storage = _storage(_aged("a.jpeg", 8), _aged("b.jpeg", 2))
        result = purge_modified_before(
            storage, 7, prefix=PREFIX, dry_run=False, now=NOW
        )
        storage.delete_many.assert_called_once_with(["2067928/image_cache/a.jpeg"])
        self.assertEqual(result["candidate_count"], 1)
        self.assertEqual(result["deleted_count"], 1)

    def test_dry_run_selects_but_never_deletes(self):
        storage = _storage(_aged("a.jpeg", 8))
        result = purge_modified_before(
            storage, 7, prefix=PREFIX, dry_run=True, now=NOW
        )
        storage.delete_many.assert_not_called()
        self.assertEqual(result["candidate_count"], 1)
        self.assertEqual(result["deleted_count"], 0)

    def test_nothing_expired_issues_no_delete_call(self):
        storage = _storage(_aged("a.jpeg", 1))
        result = purge_modified_before(
            storage, 7, prefix=PREFIX, dry_run=False, now=NOW
        )
        storage.delete_many.assert_not_called()
        self.assertEqual(result["candidate_count"], 0)

    def test_delete_errors_are_not_counted_as_deletions(self):
        storage = _storage(_aged("a.jpeg", 8), _aged("b.jpeg", 9))
        storage.delete_many.return_value = ["boom"]
        result = purge_modified_before(
            storage, 7, prefix=PREFIX, dry_run=False, now=NOW
        )
        self.assertEqual(result["candidate_count"], 2)
        self.assertEqual(result["deleted_count"], 1)
        self.assertEqual(result["errors"], ["boom"])

    def test_cutoff_is_days_before_now(self):
        result = purge_modified_before(
            _storage(), 7, prefix=PREFIX, dry_run=True, now=NOW
        )
        self.assertEqual(result["cutoff"], (NOW - timedelta(days=7)).isoformat())

    def test_sample_is_capped_but_count_is_not(self):
        """The DAG returns this dict as an XCom — the name list must stay bounded."""
        objects = [_aged(f"{i}.jpeg", 8) for i in range(SAMPLE_SIZE + 5)]
        result = purge_modified_before(
            _storage(*objects), 7, prefix=PREFIX, dry_run=True, now=NOW
        )
        self.assertEqual(result["candidate_count"], SAMPLE_SIZE + 5)
        self.assertEqual(len(result["sample"]), SAMPLE_SIZE)

    def test_root_prefix_is_refused(self):
        for prefix in ("", "/", "   "):
            with self.subTest(prefix=prefix):
                with self.assertRaises(ValueError):
                    purge_modified_before(
                        _storage(), 7, prefix=prefix, dry_run=True, now=NOW
                    )


class SuffixFilterTests(unittest.TestCase):
    """A prefix is not always a retention unit — the MSR store keeps the raw
    .MSR original next to the pickle, and only the pickle may be swept."""

    def test_suffix_excludes_other_object_kinds(self):
        storage = _storage(_aged("a.pkl", 70), _aged("a.MSR", 70))
        result = purge_modified_before(
            storage, 61, prefix="hitachi_sem/", suffix=".pkl", dry_run=False, now=NOW
        )
        storage.delete_many.assert_called_once_with(["2067928/image_cache/a.pkl"])
        self.assertEqual(result["candidate_count"], 1)

    def test_no_suffix_sweeps_every_kind(self):
        storage = _storage(_aged("a.pkl", 70), _aged("a.MSR", 70))
        result = purge_modified_before(
            storage, 61, prefix="hitachi_sem/", dry_run=True, now=NOW
        )
        self.assertEqual(result["candidate_count"], 2)

    def test_suffix_matching_nothing_deletes_nothing(self):
        """A wrong suffix must fail closed, not fall back to sweeping everything."""
        storage = _storage(_aged("a.pkl", 70))
        result = purge_modified_before(
            storage, 61, prefix="hitachi_sem/", suffix=".pickle", dry_run=False, now=NOW
        )
        storage.delete_many.assert_not_called()
        self.assertEqual(result["candidate_count"], 0)

    def test_suffix_is_reported_back(self):
        result = purge_modified_before(
            _storage(), 61, prefix="hitachi_sem/", suffix=".pkl", dry_run=True, now=NOW
        )
        self.assertEqual(result["suffix"], ".pkl")

    def test_suffix_applies_before_the_age_test(self):
        """Dry-run output must list only what a live run would touch."""
        storage = _storage(_aged("young.pkl", 2), _aged("old.MSR", 70))
        result = purge_modified_before(
            storage, 61, prefix="hitachi_sem/", suffix=".pkl", dry_run=True, now=NOW
        )
        self.assertEqual(result["sample"], [])


if __name__ == "__main__":
    unittest.main()
