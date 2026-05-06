import unittest
from unittest.mock import MagicMock

from url_shortner.service import AliasTakenError, ShortenerService


class _FakeDuplicateKeyError(Exception):
    """Stand-in for pymongo.errors.DuplicateKeyError, matched by class name."""


# Match pymongo's class name so the service's name-based detector triggers.
_FakeDuplicateKeyError.__name__ = "DuplicateKeyError"


def _build_service(*, max_retries: int = 5) -> tuple[ShortenerService, MagicMock, MagicMock, MagicMock]:
    mapping = MagicMock()
    cache = MagicMock()
    analytics = MagicMock()
    service = ShortenerService(
        mapping=mapping,
        cache=cache,
        analytics=analytics,
        max_retries=max_retries,
    )
    return service, mapping, cache, analytics


class ShortenWithAliasTests(unittest.TestCase):
    def test_valid_alias_is_persisted(self) -> None:
        service, mapping, _, _ = _build_service()
        result = service.shorten("https://wiki", alias="q3-roadmap", owner="alice")
        self.assertEqual(result, "q3-roadmap")
        mapping.create.assert_called_once_with(
            "q3-roadmap", "https://wiki", owner="alice", is_custom=True
        )

    def test_invalid_alias_raises_value_error(self) -> None:
        service, mapping, _, _ = _build_service()
        with self.assertRaises(ValueError):
            service.shorten("https://wiki", alias="bad alias")
        mapping.create.assert_not_called()

    def test_taken_alias_raises_alias_taken_error(self) -> None:
        service, mapping, _, _ = _build_service()
        mapping.create.side_effect = _FakeDuplicateKeyError("dup")
        with self.assertRaises(AliasTakenError):
            service.shorten("https://wiki", alias="q3-roadmap")


class ShortenAutoTests(unittest.TestCase):
    def test_returns_generated_code_on_first_try(self) -> None:
        service, mapping, _, _ = _build_service()
        code = service.shorten("https://wiki")
        self.assertEqual(len(code), 7)
        mapping.create.assert_called_once()

    def test_retries_on_collision_then_succeeds(self) -> None:
        service, mapping, _, _ = _build_service()
        mapping.create.side_effect = [
            _FakeDuplicateKeyError("dup"),
            _FakeDuplicateKeyError("dup"),
            None,
        ]
        code = service.shorten("https://wiki")
        self.assertEqual(len(code), 7)
        self.assertEqual(mapping.create.call_count, 3)

    def test_gives_up_after_max_retries(self) -> None:
        service, mapping, _, _ = _build_service(max_retries=3)
        mapping.create.side_effect = _FakeDuplicateKeyError("dup")
        with self.assertRaises(RuntimeError):
            service.shorten("https://wiki")
        self.assertEqual(mapping.create.call_count, 3)


class ResolveTests(unittest.TestCase):
    def test_returns_cached_url_without_hitting_mongo(self) -> None:
        service, mapping, cache, _ = _build_service()
        cache.get.return_value = "https://wiki"
        result = service.resolve("abc1234")
        self.assertEqual(result, "https://wiki")
        mapping.lookup.assert_not_called()

    def test_falls_through_to_mongo_and_repopulates_cache(self) -> None:
        service, mapping, cache, _ = _build_service()
        cache.get.return_value = None
        mapping.lookup.return_value = {"_id": "abc1234", "url": "https://wiki"}

        result = service.resolve("abc1234")

        self.assertEqual(result, "https://wiki")
        mapping.lookup.assert_called_once_with("abc1234")
        cache.set.assert_called_once_with("abc1234", "https://wiki")

    def test_returns_none_when_unknown(self) -> None:
        service, _, cache, _ = _build_service()
        cache.get.return_value = None
        service.mapping.lookup.return_value = None
        self.assertIsNone(service.resolve("nope"))


class RecordClickTests(unittest.TestCase):
    def test_delegates_meta_to_analytics(self) -> None:
        service, _, _, analytics = _build_service()
        service.record_click(
            "abc1234",
            {"ip": "10.0.0.1", "user_agent": "ua", "referrer": "ref", "owner": "alice"},
        )
        analytics.log_click.assert_called_once_with(
            "abc1234", ip="10.0.0.1", user_agent="ua", referrer="ref", owner="alice"
        )

    def test_swallows_analytics_exceptions(self) -> None:
        service, _, _, analytics = _build_service()
        analytics.log_click.side_effect = RuntimeError("opensearch down")
        # Must not raise.
        service.record_click("abc1234", {})

    def test_no_op_when_analytics_disabled(self) -> None:
        service, _, _, _ = _build_service()
        service.analytics = None
        service.record_click("abc1234", {})  # must not raise


if __name__ == "__main__":
    unittest.main()
