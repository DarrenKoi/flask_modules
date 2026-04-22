"""DataFrame persistence in Redis.

Two storage strategies are supported:

- ``records`` (default): the DataFrame is serialized as a single JSON records
  blob at one key. Simple, atomic, all-or-nothing.
- ``hash``: the DataFrame is stored as a Redis hash where each field holds one
  row as a JSON object. Supports partial reads/updates by row id.
"""

import json
from typing import Any, Literal

from .base import RDBase


def _pandas_module() -> Any:
    import pandas as pd

    return pd


class RDFrame(RDBase):
    """Save and load pandas DataFrames to/from Redis."""

    def save(
        self,
        key: str,
        dataframe: Any,
        *,
        strategy: Literal["records", "hash"] = "records",
        id_column: str | None = None,
        ex: int | None = None,
    ) -> int:
        name = self._resolve_key(key)

        if strategy == "records":
            self.client.set(name, dataframe.to_json(orient="records"), ex=ex)
            return len(dataframe)

        if strategy == "hash":
            if id_column is None:
                rows = dataframe.to_dict(orient="index")
            else:
                rows = dataframe.set_index(id_column, drop=False).to_dict(orient="index")
            fields = {str(rid): json.dumps(row) for rid, row in rows.items()}
            pipe = self.client.pipeline()
            pipe.delete(name)
            if fields:
                pipe.hset(name, mapping=fields)
            if ex is not None:
                pipe.expire(name, ex)
            pipe.execute()
            return len(fields)

        raise ValueError(f"Unknown strategy: {strategy!r}")

    def load(
        self,
        key: str,
        *,
        strategy: Literal["records", "hash"] = "records",
    ) -> Any:
        pd = _pandas_module()
        name = self._resolve_key(key)

        if strategy == "records":
            raw = self.client.get(name)
            if raw is None:
                return pd.DataFrame()
            return pd.DataFrame(json.loads(raw))

        if strategy == "hash":
            raw = self.client.hgetall(name)
            if not raw:
                return pd.DataFrame()
            return pd.DataFrame.from_dict(
                {k: json.loads(v) for k, v in raw.items()},
                orient="index",
            )

        raise ValueError(f"Unknown strategy: {strategy!r}")

    def upsert_row(self, key: str, row_id: str, row: dict[str, Any]) -> int:
        return self.client.hset(self._resolve_key(key), row_id, json.dumps(row))

    def get_row(self, key: str, row_id: str) -> dict[str, Any] | None:
        raw = self.client.hget(self._resolve_key(key), row_id)
        if raw is None:
            return None
        return json.loads(raw)

    def delete_row(self, key: str, *row_ids: str) -> int:
        return self.client.hdel(self._resolve_key(key), *row_ids)
