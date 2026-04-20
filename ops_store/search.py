"""Search-oriented wrappers for OpenSearch queries."""

from collections.abc import Sequence
from typing import Any

from .base import OSBase


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
