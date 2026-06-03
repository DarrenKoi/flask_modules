import io
import unittest
from unittest.mock import Mock

from minio_handler import MinioObject


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


if __name__ == "__main__":
    unittest.main()
