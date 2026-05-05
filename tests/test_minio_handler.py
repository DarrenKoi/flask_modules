import io
import unittest
from unittest.mock import Mock, patch

from minio_handler import MinioObject


class FakeS3Error(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class FakeFilter:
    def __init__(self, prefix: str) -> None:
        self.prefix = prefix


class FakeExpiration:
    def __init__(self, days: int) -> None:
        self.days = days


class FakeRule:
    def __init__(
        self,
        rule_id: str,
        rule_filter: FakeFilter,
        status: str,
        expiration: FakeExpiration,
    ) -> None:
        self.rule_id = rule_id
        self.rule_filter = rule_filter
        self.status = status
        self.expiration = expiration


class FakeLifecycleConfig:
    def __init__(self, rules: list[FakeRule]) -> None:
        self.rules = rules


def fake_lifecycle_imports() -> dict[str, object]:
    return {
        "LifecycleConfig": FakeLifecycleConfig,
        "Rule": FakeRule,
        "Filter": FakeFilter,
        "Expiration": FakeExpiration,
        "ENABLED": "Enabled",
    }


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

        with self.assertRaises(ValueError):
            service.delete_prefix("")

        client.list_objects.assert_not_called()
        client.remove_objects.assert_not_called()

    def test_get_lifecycle_returns_none_when_policy_is_missing(self) -> None:
        client = Mock()
        client.get_bucket_lifecycle.side_effect = FakeS3Error(
            "NoSuchLifecycleConfiguration"
        )
        service = MinioObject(client=client, bucket="bucket")

        self.assertIsNone(service.get_lifecycle())

    def test_set_expiration_creates_policy_when_policy_is_missing(self) -> None:
        client = Mock()
        client.get_bucket_lifecycle.side_effect = FakeS3Error(
            "NoSuchLifecycleConfiguration"
        )
        service = MinioObject(client=client, bucket="bucket", prefix="team-a")

        with patch("minio_handler.object._lifecycle_imports", fake_lifecycle_imports):
            config = service.set_expiration(30)

        self.assertEqual(len(config.rules), 1)
        self.assertEqual(config.rules[0].rule_id, "expire-team-a-30d")
        self.assertEqual(config.rules[0].rule_filter.prefix, "team-a/")
        client.set_bucket_lifecycle.assert_called_once_with("bucket", config)


if __name__ == "__main__":
    unittest.main()
