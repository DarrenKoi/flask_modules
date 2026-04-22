"""Redis list operations — natural fit for Python lists and queues."""

import json
from collections.abc import Iterable
from typing import Any

from .base import RDBase


class RDList(RDBase):
    """Push, pop, and range over Redis lists."""

    def push(
        self,
        key: str,
        *values: Any,
        left: bool = False,
        as_json: bool = False,
    ) -> int:
        name = self._resolve_key(key)
        payload = [json.dumps(v) for v in values] if as_json else list(values)
        return self.client.lpush(name, *payload) if left else self.client.rpush(name, *payload)

    def pop(
        self,
        key: str,
        *,
        left: bool = False,
        as_json: bool = False,
    ) -> Any:
        name = self._resolve_key(key)
        raw = self.client.lpop(name) if left else self.client.rpop(name)
        if raw is None:
            return None
        return json.loads(raw) if as_json else raw

    def get(
        self,
        key: str,
        *,
        start: int = 0,
        end: int = -1,
        as_json: bool = False,
    ) -> list[Any]:
        raw = self.client.lrange(self._resolve_key(key), start, end)
        if as_json:
            return [json.loads(v) for v in raw]
        return list(raw)

    def set_index(
        self,
        key: str,
        index: int,
        value: Any,
        *,
        as_json: bool = False,
    ) -> bool:
        payload = json.dumps(value) if as_json else value
        return self.client.lset(self._resolve_key(key), index, payload)

    def replace(
        self,
        key: str,
        values: Iterable[Any],
        *,
        as_json: bool = False,
    ) -> int:
        # Pipeline keeps delete+rpush atomic so readers never see an empty list.
        name = self._resolve_key(key)
        items = list(values)
        payload = [json.dumps(v) for v in items] if as_json else items
        pipe = self.client.pipeline()
        pipe.delete(name)
        if payload:
            pipe.rpush(name, *payload)
        pipe.execute()
        return len(payload)

    def length(self, key: str) -> int:
        return self.client.llen(self._resolve_key(key))

    def remove(self, key: str, value: Any, *, count: int = 0) -> int:
        return self.client.lrem(self._resolve_key(key), count, value)

    def trim(self, key: str, start: int, end: int) -> bool:
        return self.client.ltrim(self._resolve_key(key), start, end)
