"""Behavior tests for the Hitachi SEM date-partition purge."""

import unittest
from datetime import date
from unittest.mock import Mock

from scripts.hitachi_sem_partition_purge import (
    BUCKET,
    KINDS,
    PREFIX_ROOT,
    parent_prefixes,
    purge_hitachi_sem,
)

TODAY = date(2026, 7, 28)


class _Obj:
    def __init__(self, object_name):
        self.object_name = object_name


def _storage(partitions_by_prefix: dict) -> Mock:
    """MinioObject stand-in driving walk_date_partitions' 3-level folder walk.

    walk_date_partitions lists with recursive=False at year, then month, then
    day, so the fake answers by how many segments the requested prefix has.
    """
    storage = Mock()
    state = {"prefix": None}

    def use_prefix(p):
        state["prefix"] = p

    def _list(prefix=None, recursive=True, **kw):
        days = partitions_by_prefix.get(state["prefix"], [])
        # Each level returns DISTINCT common prefixes, as S3 does — emitting one
        # per underlying day would make the walk revisit the same subtree.
        if prefix is None:  # year level
            names = {f"{d.year}/" for d in days}
        else:
            parts = [p for p in prefix.split("/") if p]
            if len(parts) == 1:  # month level
                names = {f"{d.year}/{d.month:02d}/" for d in days if str(d.year) == parts[0]}
            else:  # day level
                names = {
                    f"{d.year}/{d.month:02d}/{d.day:02d}/"
                    for d in days
                    if [str(d.year), f"{d.month:02d}"] == parts
                }
        return [_Obj(n) for n in sorted(names)]

    storage.use_prefix.side_effect = use_prefix
    storage.list.side_effect = _list
    storage.delete_prefix.return_value = []
    return storage


class ParentPrefixTests(unittest.TestCase):
    def test_defaults_to_four_parents_under_the_namespace(self):
        prefixes = parent_prefixes()
        self.assertEqual(len(prefixes), 4)
        for p in prefixes:
            self.assertTrue(p.startswith("2067928/hitachi_sem/"), p)

    def test_kinds_narrows_to_one_data_kind_per_sensor(self):
        prefixes = parent_prefixes(kinds=("dict_pkl",))
        self.assertEqual(
            prefixes,
            ["2067928/hitachi_sem/cdsem/dict_pkl", "2067928/hitachi_sem/hvsem/dict_pkl"],
        )

    def test_raw_msr_is_reachable_only_when_asked_for(self):
        """The scheduled pickle job must never touch the .MSR originals."""
        self.assertFalse(any("raw_msr" in p for p in parent_prefixes(kinds=("dict_pkl",))))
        self.assertTrue(any("raw_msr" in p for p in parent_prefixes()))

    def test_bucket_is_the_user_bucket_not_the_namespace(self):
        """"2067928" is a prefix inside "user"; using it as a bucket 400s."""
        self.assertEqual(BUCKET, "user")
        self.assertIn("2067928", PREFIX_ROOT)


class PurgeHitachiSemTests(unittest.TestCase):
    def test_deletes_partitions_at_or_past_the_cutoff(self):
        prefix = "2067928/hitachi_sem/cdsem/dict_pkl"
        storage = _storage({prefix: [date(2026, 5, 1), date(2026, 7, 20)]})
        result = purge_hitachi_sem(
            storage,
            retention_days=61,
            kinds=("dict_pkl",),
            prefix_root=PREFIX_ROOT,
            today=TODAY,
            dry_run=False,
        )
        # cutoff = 2026-05-28; 05-01 is older, 07-20 is inside the window.
        self.assertEqual(result["cutoff"], "2026-05-28")
        storage.delete_prefix.assert_called_once_with("2026/05/01/")
        self.assertEqual(result["deleted_count"], 1)

    def test_dry_run_reports_candidates_without_deleting(self):
        prefix = "2067928/hitachi_sem/cdsem/dict_pkl"
        storage = _storage({prefix: [date(2026, 5, 1)]})
        result = purge_hitachi_sem(
            storage, retention_days=61, kinds=("dict_pkl",), today=TODAY, dry_run=True
        )
        storage.delete_prefix.assert_not_called()
        self.assertEqual(result["deleted_count"], 1)

    def test_only_selected_kinds_are_visited(self):
        storage = _storage({})
        purge_hitachi_sem(
            storage, retention_days=61, kinds=("dict_pkl",), today=TODAY, dry_run=True
        )
        visited = [c.args[0] for c in storage.use_prefix.call_args_list]
        self.assertEqual(
            visited,
            ["2067928/hitachi_sem/cdsem/dict_pkl", "2067928/hitachi_sem/hvsem/dict_pkl"],
        )

    def test_kinds_are_reported_back(self):
        result = purge_hitachi_sem(
            _storage({}), retention_days=61, kinds=("dict_pkl",), today=TODAY, dry_run=True
        )
        self.assertEqual(result["kinds"], ["dict_pkl"])

    def test_default_kinds_still_sweeps_both(self):
        """The one-off reclaim path keeps its old behavior."""
        storage = _storage({})
        purge_hitachi_sem(storage, today=TODAY, dry_run=True)
        visited = [c.args[0] for c in storage.use_prefix.call_args_list]
        self.assertEqual(len(visited), len(KINDS) * 2)


if __name__ == "__main__":
    unittest.main()
