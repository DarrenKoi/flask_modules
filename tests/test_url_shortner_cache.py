import unittest
from unittest.mock import Mock

from url_shortner.base import RedisConfig
from url_shortner.cache import KEY_PREFIX, CacheLayer


class CacheLayerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = Mock()
        self.service = CacheLayer(client=self.client, config=RedisConfig(default_ttl=60))

    def test_get_uses_prefixed_key(self) -> None:
        self.client.get.return_value = "https://example.com"
        result = self.service.get("abc1234")
        self.client.get.assert_called_once_with(f"{KEY_PREFIX}abc1234")
        self.assertEqual(result, "https://example.com")

    def test_get_returns_none_on_miss(self) -> None:
        self.client.get.return_value = None
        self.assertIsNone(self.service.get("missing"))

    def test_set_uses_default_ttl_from_config(self) -> None:
        self.service.set("abc1234", "https://example.com")
        self.client.set.assert_called_once_with(
            f"{KEY_PREFIX}abc1234",
            "https://example.com",
            ex=60,
        )

    def test_set_overrides_ttl(self) -> None:
        self.service.set("abc1234", "https://example.com", ttl=300)
        self.client.set.assert_called_once_with(
            f"{KEY_PREFIX}abc1234",
            "https://example.com",
            ex=300,
        )

    def test_invalidate_deletes_prefixed_key(self) -> None:
        self.service.invalidate("abc1234")
        self.client.delete.assert_called_once_with(f"{KEY_PREFIX}abc1234")


if __name__ == "__main__":
    unittest.main()
