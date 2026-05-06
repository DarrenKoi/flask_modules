"""Redis-backed read-through cache for ``code -> long URL`` lookups."""

from .base import RedisBase

KEY_PREFIX = "urlshortner:code:"


class CacheLayer(RedisBase):
    """Thin wrapper over Redis for caching short-code resolutions."""

    @staticmethod
    def _key(code: str) -> str:
        return f"{KEY_PREFIX}{code}"

    def get(self, code: str) -> str | None:
        return self.client.get(self._key(code))

    def set(self, code: str, url: str, *, ttl: int | None = None) -> None:
        effective_ttl = ttl if ttl is not None else (
            self.config.default_ttl if self.config is not None else 86400
        )
        self.client.set(self._key(code), url, ex=effective_ttl)

    def invalidate(self, code: str) -> None:
        self.client.delete(self._key(code))
