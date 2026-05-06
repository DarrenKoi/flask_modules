import unittest
from unittest.mock import MagicMock

from url_shortner.app import create_app
from url_shortner.service import AliasTakenError, ShortenerService


def _build_app(base_url: str = "http://short.local") -> tuple[MagicMock, MagicMock, MagicMock, ShortenerService, object]:
    mapping = MagicMock()
    cache = MagicMock()
    analytics = MagicMock()
    service = ShortenerService(mapping=mapping, cache=cache, analytics=analytics)
    app = create_app(service=service, base_url=base_url)
    app.config.update(TESTING=True)
    return mapping, cache, analytics, service, app.test_client()


class IndexRouteTests(unittest.TestCase):
    def test_index_renders_form(self) -> None:
        _, _, _, _, client = _build_app()
        response = client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Internal URL Shortener", response.data)


class ShortenRouteTests(unittest.TestCase):
    def test_returns_short_url_on_success(self) -> None:
        mapping, _, _, _, client = _build_app()
        # No collision, mapping.create returns normally.
        response = client.post("/shorten", json={"url": "https://wiki"})
        self.assertEqual(response.status_code, 201)
        body = response.get_json()
        self.assertEqual(len(body["code"]), 7)
        self.assertTrue(body["short_url"].startswith("http://short.local/"))
        mapping.create.assert_called_once()

    def test_missing_url_returns_400(self) -> None:
        _, _, _, _, client = _build_app()
        response = client.post("/shorten", json={})
        self.assertEqual(response.status_code, 400)

    def test_invalid_alias_returns_400(self) -> None:
        _, _, _, _, client = _build_app()
        response = client.post("/shorten", json={"url": "https://wiki", "alias": "x"})
        self.assertEqual(response.status_code, 400)

    def test_taken_alias_returns_409(self) -> None:
        mapping, _, _, _, client = _build_app()

        class _Dup(Exception):
            pass
        _Dup.__name__ = "DuplicateKeyError"
        mapping.create.side_effect = _Dup("dup")

        response = client.post("/shorten", json={"url": "https://wiki", "alias": "q3-roadmap"})
        self.assertEqual(response.status_code, 409)


class FollowRouteTests(unittest.TestCase):
    def test_redirects_to_long_url_and_logs_click(self) -> None:
        mapping, cache, analytics, _, client = _build_app()
        cache.get.return_value = None
        mapping.lookup.return_value = {"_id": "abc1234", "url": "https://wiki"}

        response = client.get("/abc1234", follow_redirects=False)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "https://wiki")
        analytics.log_click.assert_called_once()

    def test_returns_404_when_unknown(self) -> None:
        mapping, cache, _, _, client = _build_app()
        cache.get.return_value = None
        mapping.lookup.return_value = None

        response = client.get("/nope")
        self.assertEqual(response.status_code, 404)


if __name__ == "__main__":
    unittest.main()
