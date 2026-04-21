"""Search-oriented wrappers for OpenSearch queries."""

from collections.abc import Sequence
from functools import lru_cache
from typing import Any

from .base import OSBase


@lru_cache(maxsize=1)
def _pandas_module() -> Any | None:
    try:
        import pandas as pd
    except ImportError:
        return None
    return pd


def _hit_to_record(hit: dict[str, Any], *, include_meta: bool) -> dict[str, Any]:
    source = hit.get("_source")
    if isinstance(source, dict):
        record = dict(source)
    else:
        fields = hit.get("fields")
        record = dict(fields) if isinstance(fields, dict) else {}

    if include_meta:
        for key in ("_id", "_index", "_score"):
            if key in hit:
                record[key] = hit[key]

    return record


def _hits_from_result(result: dict[str, Any]) -> list[dict[str, Any]]:
    hits = result.get("hits")
    raw_hits = hits.get("hits") if isinstance(hits, dict) else []
    return [hit for hit in raw_hits if isinstance(hit, dict)]


def _records_from_hits(
    hits: Sequence[dict[str, Any]],
    *,
    include_meta: bool,
) -> list[dict[str, Any]]:
    return [_hit_to_record(hit, include_meta=include_meta) for hit in hits]


def _require_pandas() -> Any:
    pandas = _pandas_module()
    if pandas is None:
        raise ImportError(
            "pandas is required for OSSearch DataFrame helpers. "
            "Install pandas to use these helpers."
        )
    return pandas


def _records_to_dataframe(records: list[dict[str, Any]]) -> Any:
    pandas = _require_pandas()
    return pandas.DataFrame(records)


class OSSearch(OSBase):
    """Class-based query helper for lexical, vector, and raw searches."""

    def search_raw(
        self,
        body: dict[str, Any],
        *,
        index: str | None = None,
    ) -> dict[str, Any]:
        name = self._resolve_index(index)
        result = self.client.search(index=name, body=body)
        return self._log_result("search_raw", result, index=name)

    def to_dataframe(
        self,
        result: dict[str, Any],
        *,
        include_meta: bool = False,
    ) -> Any:
        records = _records_from_hits(
            _hits_from_result(result),
            include_meta=include_meta,
        )
        return _records_to_dataframe(records)

    def _search_all_hits(
        self,
        body: dict[str, Any],
        *,
        index: str | None = None,
        batch_size: int = 1000,
        scroll: str = "2m",
    ) -> tuple[str, list[dict[str, Any]]]:
        name = self._resolve_index(index)
        request_body = dict(body)
        request_body["size"] = batch_size

        response = self.client.search(index=name, body=request_body, scroll=scroll)
        scroll_id = response.get("_scroll_id")
        page_hits = _hits_from_result(response)
        all_hits = list(page_hits)

        try:
            while page_hits and scroll_id is not None:
                response = self.client.scroll(scroll_id=scroll_id, scroll=scroll)
                next_scroll_id = response.get("_scroll_id")
                if next_scroll_id is not None:
                    scroll_id = next_scroll_id
                page_hits = _hits_from_result(response)
                all_hits.extend(page_hits)
        finally:
            if scroll_id is not None:
                self.client.clear_scroll(scroll_id=scroll_id)

        return name, all_hits

    def search_dataframe(
        self,
        body: dict[str, Any],
        *,
        index: str | None = None,
        include_meta: bool = False,
    ) -> Any:
        result = self.search_raw(body, index=index)
        return self.to_dataframe(result, include_meta=include_meta)

    def search_dataframe_all(
        self,
        body: dict[str, Any],
        *,
        index: str | None = None,
        batch_size: int = 1000,
        scroll: str = "2m",
        include_meta: bool = False,
    ) -> Any:
        _require_pandas()
        name, all_hits = self._search_all_hits(
            body,
            index=index,
            batch_size=batch_size,
            scroll=scroll,
        )
        dataframe = _records_to_dataframe(
            _records_from_hits(all_hits, include_meta=include_meta)
        )
        self._log_result(
            "search_dataframe_all",
            {"count": len(all_hits)},
            index=name,
            batch_size=batch_size,
            scroll=scroll,
        )
        return dataframe

    def count(
        self,
        query: dict[str, Any] | None = None,
        *,
        index: str | None = None,
    ) -> dict[str, Any]:
        body = {"query": query} if query else {}
        name = self._resolve_index(index)
        result = self.client.count(index=name, body=body)
        return self._log_result("count", result, index=name)

    def match(
        self,
        field: str,
        query: str,
        *,
        index: str | None = None,
        size: int = 10,
    ) -> dict[str, Any]:
        body = {"query": {"match": {field: query}}, "size": size}
        return self.search_raw(body, index=index)

    def match_dataframe(
        self,
        field: str,
        query: str,
        *,
        index: str | None = None,
        size: int = 10,
        include_meta: bool = False,
    ) -> Any:
        result = self.match(field, query, index=index, size=size)
        return self.to_dataframe(result, include_meta=include_meta)

    def match_dataframe_all(
        self,
        field: str,
        query: str,
        *,
        index: str | None = None,
        batch_size: int = 1000,
        scroll: str = "2m",
        include_meta: bool = False,
    ) -> Any:
        body = {"query": {"match": {field: query}}}
        return self.search_dataframe_all(
            body,
            index=index,
            batch_size=batch_size,
            scroll=scroll,
            include_meta=include_meta,
        )

    def term(
        self,
        field: str,
        value: Any,
        *,
        index: str | None = None,
        size: int = 10,
    ) -> dict[str, Any]:
        body = {"query": {"term": {field: value}}, "size": size}
        return self.search_raw(body, index=index)

    def bool(
        self,
        *,
        must: list[dict[str, Any]] | None = None,
        should: list[dict[str, Any]] | None = None,
        filter: list[dict[str, Any]] | None = None,
        must_not: list[dict[str, Any]] | None = None,
        index: str | None = None,
        size: int = 10,
    ) -> dict[str, Any]:
        bool_clause: dict[str, Any] = {}
        if must:
            bool_clause["must"] = must
        if should:
            bool_clause["should"] = should
        if filter:
            bool_clause["filter"] = filter
        if must_not:
            bool_clause["must_not"] = must_not

        body = {"query": {"bool": bool_clause}, "size": size}
        return self.search_raw(body, index=index)

    def multi_match(
        self,
        query: str,
        fields: Sequence[str],
        *,
        index: str | None = None,
        size: int = 10,
        match_type: str = "best_fields",
    ) -> dict[str, Any]:
        body = {
            "query": {
                "multi_match": {
                    "query": query,
                    "fields": list(fields),
                    "type": match_type,
                }
            },
            "size": size,
        }
        return self.search_raw(body, index=index)

    def knn(
        self,
        field: str,
        vector: Sequence[float],
        *,
        index: str | None = None,
        k: int = 5,
        size: int = 10,
        filters: Sequence[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        filter_list = list(filters or [])
        knn_query = {"knn": {field: {"vector": list(vector), "k": k}}}
        if filter_list:
            body = {
                "query": {
                    "bool": {
                        "must": [knn_query],
                        "filter": filter_list,
                    }
                },
                "size": size,
            }
        else:
            body = {"query": knn_query, "size": size}

        return self.search_raw(body, index=index)

    def hybrid(
        self,
        query: str,
        *,
        text_field: str,
        vector_field: str,
        vector: Sequence[float],
        index: str | None = None,
        k: int = 5,
        size: int = 10,
        filters: Sequence[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        bool_clause: dict[str, Any] = {
            "should": [
                {"match": {text_field: query}},
                {"knn": {vector_field: {"vector": list(vector), "k": k}}},
            ],
            "minimum_should_match": 1,
        }

        filter_list = list(filters or [])
        if filter_list:
            bool_clause["filter"] = filter_list

        body = {"query": {"bool": bool_clause}, "size": size}
        return self.search_raw(body, index=index)

    def aggregate(
        self,
        aggregations: dict[str, Any],
        *,
        query: dict[str, Any] | None = None,
        index: str | None = None,
        size: int = 0,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"aggs": aggregations, "size": size}
        if query is not None:
            body["query"] = query
        return self.search_raw(body, index=index)
