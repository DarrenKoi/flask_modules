import io
import unittest
from unittest.mock import Mock

from minio_handler import MinioObject


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


if __name__ == "__main__":
    unittest.main()
