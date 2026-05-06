"""High-level orchestration of the URL shortener: shorten / resolve / record."""

from typing import Any

from .analytics import ClickAnalytics
from .cache import CacheLayer
from .codegen import generate_code, is_valid_alias
from .mapping import URLMapping


class AliasTakenError(Exception):
    """Raised when a custom alias is already in use."""


def _is_duplicate_key_error(exc: Exception) -> bool:
    """Detect pymongo's DuplicateKeyError without a hard import dependency."""

    return type(exc).__name__ == "DuplicateKeyError"


class ShortenerService:
    """Coordinate the mapping store, hot cache, and click analytics."""

    def __init__(
        self,
        mapping: URLMapping,
        cache: CacheLayer,
        analytics: ClickAnalytics | None = None,
        *,
        max_retries: int = 5,
    ) -> None:
        self.mapping = mapping
        self.cache = cache
        self.analytics = analytics
        self.max_retries = max_retries

    def shorten(
        self,
        url: str,
        *,
        alias: str | None = None,
        owner: str | None = None,
    ) -> str:
        if alias is not None:
            if not is_valid_alias(alias):
                raise ValueError(
                    "alias must be 2-32 chars of letters, digits, '-' or '_'"
                )
            try:
                self.mapping.create(alias, url, owner=owner, is_custom=True)
            except Exception as exc:
                if _is_duplicate_key_error(exc):
                    raise AliasTakenError(alias) from exc
                raise
            return alias

        for _ in range(self.max_retries):
            code = generate_code()
            try:
                self.mapping.create(code, url, owner=owner, is_custom=False)
            except Exception as exc:
                if _is_duplicate_key_error(exc):
                    continue
                raise
            return code

        raise RuntimeError(
            f"failed to allocate a unique short code after {self.max_retries} attempts"
        )

    def resolve(self, code: str) -> str | None:
        cached = self.cache.get(code)
        if cached is not None:
            return cached

        record = self.mapping.lookup(code)
        if record is None:
            return None

        url = record["url"]
        self.cache.set(code, url)
        return url

    def record_click(self, code: str, meta: dict[str, Any] | None = None) -> None:
        if self.analytics is None:
            return
        meta = meta or {}
        try:
            self.analytics.log_click(
                code,
                ip=meta.get("ip"),
                user_agent=meta.get("user_agent"),
                referrer=meta.get("referrer"),
                owner=meta.get("owner"),
            )
        except Exception:
            # Analytics outage must not break redirects.
            pass
