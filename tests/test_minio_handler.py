import io
import unittest
from datetime import date
from types import SimpleNamespace
from unittest.mock import Mock, patch

from minio_handler import DateFolder, DeleteOlderResult, MinioObject


def _make_get_service(bodies: dict[str, bytes], *, fail: set[str] = frozenset()):
    """MinioObject whose client.get_object returns per-key bodies or raises."""

    def get_object(bucket_name, object_name, **kwargs):
        if object_name in fail:
            raise RuntimeError(f"boom: {object_name}")
        response = Mock()
        response.read.return_value = bodies[object_name]
        return response

    client = Mock()
    client.get_object.side_effect = get_object
    service = MinioObject(client=client, bucket="bucket")
    service.use_prefix(None)   # ignore any minio_config.py PREFIX
    return service


class MinioObjectSafetyTests(unittest.TestCase):
    def test_put_rejects_unknown_stream_length_without_part_size(self) -> None:
        client = Mock()
        service = MinioObject(client=client, bucket="bucket")

        with self.assertRaises(ValueError):
            service.put("data.bin", io.BytesIO(b"abc"))

        client.put_object.assert_not_called()

    def test_put_allows_unknown_stream_length_with_part_size(self) -> None:
        client = Mock()
        service = MinioObject(client=client, bucket="bucket")

        service.put("data.bin", io.BytesIO(b"abc"), part_size=10 * 1024 * 1024)

        client.put_object.assert_called_once()
        self.assertEqual(client.put_object.call_args.args[3], -1)
        self.assertEqual(client.put_object.call_args.kwargs["part_size"], 10 * 1024 * 1024)

    def test_delete_prefix_rejects_empty_scope_without_default_prefix(self) -> None:
        client = Mock()
        service = MinioObject(client=client, bucket="bucket")
        service.use_prefix(None)   # ignore any minio_config.py PREFIX

        with self.assertRaises(ValueError):
            service.delete_prefix("")

        client.list_objects.assert_not_called()
        client.remove_objects.assert_not_called()

    def test_delete_many_accepts_full_keys_returned_by_list(self) -> None:
        client = Mock()
        client.remove_objects.return_value = []
        service = MinioObject(
            client=client,
            bucket="bucket",
            prefix="measurements",
        )

        with patch(
            "minio_handler.object._delete_object_class",
            return_value=lambda name: SimpleNamespace(name=name),
        ):
            service.delete_many(
                [
                    "measurements/shot01.jpeg",
                    "measurements/sub/shot02.jpeg",
                    "shot03.jpeg",
                ]
            )

        _, targets = client.remove_objects.call_args.args
        self.assertEqual(
            [target.name for target in targets],
            [
                "measurements/shot01.jpeg",
                "measurements/sub/shot02.jpeg",
                "measurements/shot03.jpeg",
            ],
        )

    def test_list_keeps_explicit_name_prefix_without_adding_slash(self) -> None:
        client = Mock()
        service = MinioObject(
            client=client,
            bucket="bucket",
            prefix="measurements",
        )

        service.list("shot")

        self.assertEqual(
            client.list_objects.call_args.kwargs["prefix"],
            "measurements/shot",
        )


class MinioGetManyTests(unittest.TestCase):
    def test_get_many_returns_all_bodies(self) -> None:
        bodies = {"a": b"AAA", "b": b"BBB", "c": b"CCC"}
        service = _make_get_service(bodies)

        result = service.get_many(["a", "b", "c"])

        self.assertEqual(result.objects, bodies)
        self.assertEqual(result.errors, {})

    def test_get_many_skips_failures_and_records_them(self) -> None:
        bodies = {"a": b"AAA", "c": b"CCC"}
        service = _make_get_service(bodies, fail={"b"})

        result = service.get_many(["a", "b", "c"])

        self.assertEqual(result.objects, {"a": b"AAA", "c": b"CCC"})
        self.assertIn("b", result.errors)
        self.assertIsInstance(result.errors["b"], RuntimeError)

    def test_get_many_preserves_requested_key_order(self) -> None:
        bodies = {"a": b"AAA", "b": b"BBB", "c": b"CCC"}
        service = _make_get_service(bodies)

        result = service.get_many(["c", "a", "b"])

        self.assertEqual(list(result.objects), ["c", "a", "b"])

    def test_get_many_applies_decode_to_each_body(self) -> None:
        bodies = {"a": b"1", "b": b"22", "c": b"333"}
        service = _make_get_service(bodies)

        result = service.get_many(["a", "b", "c"], decode=len)

        self.assertEqual(result.objects, {"a": 1, "b": 2, "c": 3})
        self.assertEqual(result.errors, {})

    def test_get_many_decode_failure_lands_in_errors(self) -> None:
        bodies = {"a": b"ok", "b": b"bad"}

        def decode(body: bytes) -> str:
            if body == b"bad":
                raise ValueError("cannot decode")
            return body.decode()

        service = _make_get_service(bodies)

        result = service.get_many(["a", "b"], decode=decode)

        self.assertEqual(result.objects, {"a": "ok"})
        self.assertIsInstance(result.errors["b"], ValueError)

    def test_get_many_empty_keys_makes_no_calls(self) -> None:
        service = _make_get_service({})

        result = service.get_many([])

        self.assertEqual(result.objects, {})
        self.assertEqual(result.errors, {})
        service.client.get_object.assert_not_called()


def _make_list_service(keys: list[str], *, remove_errors: list = None):
    """MinioObject whose client.list_objects yields objects for ``keys``."""

    client = Mock()
    client.list_objects.return_value = [
        Mock(object_name=key) for key in keys
    ]
    client.remove_objects.return_value = list(remove_errors or [])
    service = MinioObject(client=client, bucket="bucket")
    service.use_prefix(None)   # ignore any minio_config.py PREFIX
    return service


class MinioDeleteMatchingTests(unittest.TestCase):
    def test_delete_matching_deletes_only_matching_keys(self) -> None:
        keys = [
            "sem/wafer_2026-06-11_a.parquet",
            "sem/wafer_2026-06-12_b.parquet",
            "sem/wafer_2026-06-11_c.parquet",
        ]
        service = _make_list_service(keys)

        result = service.delete_matching(lambda k: "2026-06-11" in k)

        self.assertEqual(result, [])
        service.client.remove_objects.assert_called_once()
        bucket_name, targets = service.client.remove_objects.call_args.args
        self.assertEqual(bucket_name, "bucket")
        deleted = {t.name for t in targets}
        self.assertEqual(
            deleted,
            {"sem/wafer_2026-06-11_a.parquet", "sem/wafer_2026-06-11_c.parquet"},
        )

    def test_delete_matching_no_match_makes_no_remove_call(self) -> None:
        service = _make_list_service(["a.txt", "b.txt"])

        result = service.delete_matching(lambda k: k.endswith(".parquet"))

        self.assertEqual(result, [])
        service.client.remove_objects.assert_not_called()

    def test_delete_matching_narrows_listing_with_prefix(self) -> None:
        service = _make_list_service(["sem/2026/06/11/x"])

        service.delete_matching(lambda k: True, prefix="sem")

        kwargs = service.client.list_objects.call_args.kwargs
        self.assertEqual(kwargs["prefix"], "sem/")
        self.assertTrue(kwargs["recursive"])

    def test_delete_matching_returns_remove_error_entries(self) -> None:
        service = _make_list_service(["a.parquet"], remove_errors=["boom"])

        result = service.delete_matching(lambda k: True)

        self.assertEqual(result, ["boom"])


class _FakeMinio:
    """Emulate MinIO ``list_objects`` common-prefix rollup over a flat key set.

    A non-recursive listing of ``prefix`` yields one entry per immediate child:
    a folder shows up as a common prefix ending in ``/``; a leaf file shows up
    as its full key. A recursive listing yields every leaf under ``prefix``.
    """

    def __init__(self, keys: list[str], *, remove_errors: list = None):
        self.keys = sorted(keys)
        self.removed: list[str] = []
        self.remove_errors = list(remove_errors or [])
        self.list_calls: list[dict] = []

    def list_objects(
        self, *, bucket_name, prefix="", recursive=False, start_after=None
    ):
        self.list_calls.append({"prefix": prefix, "recursive": recursive})
        if recursive:
            return [
                Mock(object_name=k) for k in self.keys if k.startswith(prefix)
            ]
        out = []
        seen = set()
        for k in self.keys:
            if not k.startswith(prefix):
                continue
            rest = k[len(prefix):]
            if "/" in rest:
                entry = prefix + rest.split("/", 1)[0] + "/"
            else:
                entry = k
            if entry not in seen:
                seen.add(entry)
                out.append(Mock(object_name=entry))
        return out

    def remove_objects(self, bucket_name, targets):
        targets = list(targets)
        self.removed.extend(t.name for t in targets)
        return list(self.remove_errors)


def _make_date_service(keys: list[str], *, remove_errors: list = None):
    client = _FakeMinio(keys, remove_errors=remove_errors)
    service = MinioObject(client=client, bucket="bucket")
    service.use_prefix(None)   # ignore any minio_config.py PREFIX
    return service


def _tree(*days: str) -> list[str]:
    """Build payload keys under ``hitachi_sem/cdsem/one/<day>/data/x.parquet``."""

    base = "hitachi_sem/cdsem/one"
    return [f"{base}/{d}/data/x.parquet" for d in days]


class MinioListDateFoldersTests(unittest.TestCase):
    def test_walks_three_levels_and_parses_dates_sorted(self) -> None:
        service = _make_date_service(
            _tree("2026/06/11", "2026/05/17", "2026/06/02")
        )

        folders = service.list_date_folders("hitachi_sem/cdsem/one")

        self.assertEqual(
            [f.date for f in folders],
            [date(2026, 5, 17), date(2026, 6, 2), date(2026, 6, 11)],
        )
        self.assertEqual(
            folders[-1].path, "hitachi_sem/cdsem/one/2026/06/11/"
        )
        self.assertIsInstance(folders[0], DateFolder)

    def test_listings_are_non_recursive(self) -> None:
        service = _make_date_service(_tree("2026/06/11"))

        service.list_date_folders("hitachi_sem/cdsem/one")

        self.assertTrue(
            all(not c["recursive"] for c in service.client.list_calls)
        )

    def test_skips_non_date_folders(self) -> None:
        keys = _tree("2026/06/11") + [
            "hitachi_sem/cdsem/one/latest/data/x.parquet",   # bad year
            "hitachi_sem/cdsem/one/2026/ab/01/data/x.parquet",   # bad month
            "hitachi_sem/cdsem/one/2026/06/9/data/x.parquet",    # not padded
            "hitachi_sem/cdsem/one/2026/13/01/data/x.parquet",   # invalid month
        ]
        service = _make_date_service(keys)

        folders = service.list_date_folders("hitachi_sem/cdsem/one")

        self.assertEqual([f.date for f in folders], [date(2026, 6, 11)])

    def test_anchor_composes_with_default_prefix(self) -> None:
        client = _FakeMinio(
            [f"kpo/{k}" for k in _tree("2026/06/11")]
        )
        service = MinioObject(client=client, bucket="bucket")
        service.use_prefix("kpo")

        folders = service.list_date_folders("hitachi_sem/cdsem/one")

        self.assertEqual(
            folders[0].path, "kpo/hitachi_sem/cdsem/one/2026/06/11/"
        )


class MinioDeleteOlderThanTests(unittest.TestCase):
    def setUp(self) -> None:
        patcher = patch(
            "minio_handler.object._today_kst", return_value=date(2026, 6, 17)
        )
        self.addCleanup(patcher.stop)
        patcher.start()

    def test_selects_only_folders_before_cutoff(self) -> None:
        # cutoff = 2026-06-17 - 30 days = 2026-05-18; keep >= 2026-05-18.
        service = _make_date_service(
            _tree("2026/05/17", "2026/05/18", "2026/06/11")
        )

        result = service.delete_older_than(30, "hitachi_sem/cdsem/one")

        self.assertIsInstance(result, DeleteOlderResult)
        self.assertEqual([f.date for f in result.folders], [date(2026, 5, 17)])
        self.assertEqual(
            service.client.removed,
            ["hitachi_sem/cdsem/one/2026/05/17/data/x.parquet"],
        )

    def test_dry_run_selects_but_deletes_nothing(self) -> None:
        service = _make_date_service(_tree("2026/05/17", "2026/06/11"))

        result = service.delete_older_than(
            30, "hitachi_sem/cdsem/one", dry_run=True
        )

        self.assertEqual([f.date for f in result.folders], [date(2026, 5, 17)])
        self.assertEqual(result.errors, [])
        self.assertEqual(service.client.removed, [])

    def test_deletes_each_day_subtree_server_side_narrowed(self) -> None:
        service = _make_date_service(_tree("2026/05/01", "2026/05/02"))

        service.delete_older_than(30, "hitachi_sem/cdsem/one")

        recursive_prefixes = {
            c["prefix"] for c in service.client.list_calls if c["recursive"]
        }
        self.assertEqual(
            recursive_prefixes,
            {
                "hitachi_sem/cdsem/one/2026/05/01/",
                "hitachi_sem/cdsem/one/2026/05/02/",
            },
        )

    def test_keeps_everything_when_nothing_is_old(self) -> None:
        service = _make_date_service(_tree("2026/06/11", "2026/06/17"))

        result = service.delete_older_than(30, "hitachi_sem/cdsem/one")

        self.assertEqual(result.folders, [])
        self.assertEqual(service.client.removed, [])

    def test_surfaces_remove_error_entries(self) -> None:
        service = _make_date_service(
            _tree("2026/05/01"), remove_errors=["boom"]
        )

        result = service.delete_older_than(30, "hitachi_sem/cdsem/one")

        self.assertEqual(result.errors, ["boom"])


class ConnectionCheckTests(unittest.TestCase):
    def test_ping_probes_default_bucket(self) -> None:
        client = Mock()
        client.bucket_exists.return_value = True
        service = MinioObject(client=client, bucket="bucket")

        self.assertTrue(service.ping())
        client.bucket_exists.assert_called_once_with("bucket")
        client.list_buckets.assert_not_called()

    def test_ping_returns_false_when_endpoint_unreachable(self) -> None:
        client = Mock()
        client.bucket_exists.side_effect = ConnectionError("connection refused")
        service = MinioObject(client=client, bucket="bucket")

        self.assertFalse(service.ping())

    def test_ping_is_true_for_a_reachable_endpoint_missing_the_bucket(self) -> None:
        client = Mock()
        client.bucket_exists.return_value = False
        service = MinioObject(client=client, bucket="bucket")

        status = service.check_connection()

        self.assertTrue(service.ping())
        self.assertTrue(status.ok)
        self.assertFalse(status.detail["bucket_exists"])

    def test_check_connection_uses_the_bucket_from_minio_config(self) -> None:
        client = Mock()
        client.bucket_exists.return_value = True
        with patch(
            "minio_handler.base._module_values", return_value={"bucket": "cfg-bucket"}
        ):
            service = MinioObject(client=client)

        status = service.check_connection()

        self.assertTrue(status.ok)
        self.assertEqual(status.detail["bucket"], "cfg-bucket")
        client.bucket_exists.assert_called_once_with("cfg-bucket")
        client.list_buckets.assert_not_called()

    def test_check_connection_reports_a_missing_bucket_name(self) -> None:
        client = Mock()
        with patch("minio_handler.base._module_values", return_value={}):
            service = MinioObject(client=client)

        status = service.check_connection()

        self.assertFalse(status)
        self.assertIn("bucket name is required", status.error)
        client.bucket_exists.assert_not_called()
        client.list_buckets.assert_not_called()

    def test_check_connection_reports_error_without_raising(self) -> None:
        client = Mock()
        client.bucket_exists.side_effect = PermissionError("access denied")
        service = MinioObject(client=client, bucket="bucket")

        status = service.check_connection()

        self.assertFalse(status)
        self.assertEqual(status.error, "PermissionError: access denied")
        self.assertEqual(status.detail, {})
        self.assertGreaterEqual(status.elapsed_ms, 0)

    def test_check_connection_accepts_an_explicit_bucket(self) -> None:
        client = Mock()
        client.bucket_exists.return_value = True
        service = MinioObject(client=client, bucket="bucket")

        status = service.check_connection("other")

        self.assertEqual(status.detail["bucket"], "other")
        client.bucket_exists.assert_called_once_with("other")


if __name__ == "__main__":
    unittest.main()
