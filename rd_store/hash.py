"""Redis hash operations — natural fit for Python dicts."""

import json
from collections.abc import Mapping
from typing import Any

from .base import RDBase


class RDHash(RDBase):
    """Store and retrieve dicts as Redis hashes (field-level CRUD).

    With ``as_json=True`` each field value is JSON-encoded on write and decoded
    on read — use this when field values are dicts, lists, or other non-scalar.
    """

    def set(
        self,
        key: str,
        mapping: Mapping[str, Any],
        *,
        as_json: bool = False,
    ) -> int:
        if as_json:
            payload = {str(k): json.dumps(v) for k, v in mapping.items()}
        else:
            payload = {str(k): v for k, v in mapping.items()}
        return self.client.hset(self._resolve_key(key), mapping=payload)

    def set_field(
        self,
        key: str,
        field: str,
        value: Any,
        *,
        as_json: bool = False,
    ) -> int:
        payload = json.dumps(value) if as_json else value
        return self.client.hset(self._resolve_key(key), field, payload)

    def get(self, key: str, *, as_json: bool = False) -> dict[str, Any]:
        raw = self.client.hgetall(self._resolve_key(key))
        if not raw:
            return {}
        if as_json:
            return {k: json.loads(v) for k, v in raw.items()}
        return dict(raw)

    def get_field(self, key: str, field: str, *, as_json: bool = False) -> Any:
        raw = self.client.hget(self._resolve_key(key), field)
        if raw is None:
            return None
        return json.loads(raw) if as_json else raw

    def get_fields(
        self,
        key: str,
        fields: list[str],
        *,
        as_json: bool = False,
    ) -> dict[str, Any]:
        values = self.client.hmget(self._resolve_key(key), fields)
        result: dict[str, Any] = {}
        for f, v in zip(fields, values):
            if v is None:
                result[f] = None
            elif as_json:
                result[f] = json.loads(v)
            else:
                result[f] = v
        return result

    def delete_field(self, key: str, *fields: str) -> int:
        return self.client.hdel(self._resolve_key(key), *fields)

    def field_exists(self, key: str, field: str) -> bool:
        return bool(self.client.hexists(self._resolve_key(key), field))

    def fields(self, key: str) -> list[str]:
        return list(self.client.hkeys(self._resolve_key(key)))

    def values(self, key: str, *, as_json: bool = False) -> list[Any]:
        raw = self.client.hvals(self._resolve_key(key))
        if as_json:
            return [json.loads(v) for v in raw]
        return list(raw)

    def incr(self, key: str, field: str, amount: int = 1) -> int:
        return self.client.hincrby(self._resolve_key(key), field, amount)

    def length(self, key: str) -> int:
        return self.client.hlen(self._resolve_key(key))
