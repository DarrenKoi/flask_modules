"""Key-value CRUD with optional JSON serialization for dicts and lists."""

import json
from typing import Any

from .base import RDBase


class RDValue(RDBase):
    """Simple string/JSON key-value operations.

    With ``as_json=True`` values are serialized via ``json.dumps`` on write and
    parsed via ``json.loads`` on read — the natural path for dicts and lists.
    """

    def set(
        self,
        key: str,
        value: Any,
        *,
        ex: int | None = None,
        px: int | None = None,
        nx: bool = False,
        xx: bool = False,
        as_json: bool = False,
    ) -> bool:
        payload = json.dumps(value) if as_json else value
        return self.client.set(
            self._resolve_key(key), payload, ex=ex, px=px, nx=nx, xx=xx
        )

    def get(self, key: str, *, as_json: bool = False) -> Any:
        raw = self.client.get(self._resolve_key(key))
        if raw is None:
            return None
        return json.loads(raw) if as_json else raw

    def incr(self, key: str, amount: int = 1) -> int:
        return self.client.incrby(self._resolve_key(key), amount)

    def decr(self, key: str, amount: int = 1) -> int:
        return self.client.decrby(self._resolve_key(key), amount)

    def scan(self, pattern: str = "*", *, count: int | None = None) -> list[str]:
        """Return matching keys via SCAN (non-blocking, unlike KEYS)."""

        return list(self.client.scan_iter(match=self._resolve_key(pattern), count=count))
