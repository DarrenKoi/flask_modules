"""Document CRUD and bulk operations."""

from collections.abc import Iterable, Iterator, Mapping, Sequence
from typing import Any

from .base import OSBase


def _bulk_helper() -> Any:
    from opensearchpy import helpers

    return helpers.bulk


class OSDoc(OSBase):
    """Class-based wrapper for single-document and bulk document operations."""

    def _run_bulk(
        self,
        actions: Iterable[dict[str, Any]],
        *,
        chunk_size: int,
        refresh: bool,
        raise_on_error: bool,
    ) -> tuple[int, list[Any]]:
        return _bulk_helper()(
            self.client,
            actions,
            chunk_size=chunk_size,
            refresh=refresh,
            raise_on_error=raise_on_error,
        )

    def index(
        self,
        document: Mapping[str, Any],
        *,
        doc_id: str | None = None,
        index: str | None = None,
        refresh: str | None = None,
    ) -> dict[str, Any]:
        name = self._resolve_index(index)
        kwargs: dict[str, Any] = {
            "index": name,
            "body": dict(document),
        }
        if doc_id is not None:
            kwargs["id"] = doc_id
        if refresh is not None:
            kwargs["refresh"] = refresh
        result = self.client.index(**kwargs)
        return self._log_result("index_doc", result, index=name, doc_id=doc_id)

    def get(self, doc_id: str, *, index: str | None = None) -> dict[str, Any]:
        name = self._resolve_index(index)
        result = self.client.get(index=name, id=doc_id)
        return self._log_result("get_doc", result, index=name, doc_id=doc_id)

    def update(
        self,
        doc_id: str,
        document: Mapping[str, Any],
        *,
        index: str | None = None,
        refresh: str | None = None,
    ) -> dict[str, Any]:
        name = self._resolve_index(index)
        kwargs: dict[str, Any] = {
            "index": name,
            "id": doc_id,
            "body": {"doc": dict(document)},
        }
        if refresh is not None:
            kwargs["refresh"] = refresh
        result = self.client.update(**kwargs)
        return self._log_result("update_doc", result, index=name, doc_id=doc_id)

    def upsert(
        self,
        doc_id: str,
        document: Mapping[str, Any],
        *,
        index: str | None = None,
        refresh: str | None = None,
    ) -> dict[str, Any]:
        name = self._resolve_index(index)
        kwargs: dict[str, Any] = {
            "index": name,
            "id": doc_id,
            "body": {"doc": dict(document), "doc_as_upsert": True},
        }
        if refresh is not None:
            kwargs["refresh"] = refresh
        result = self.client.update(**kwargs)
        return self._log_result("upsert_doc", result, index=name, doc_id=doc_id)

    def delete(
        self,
        doc_id: str,
        *,
        index: str | None = None,
        refresh: str | None = None,
    ) -> dict[str, Any]:
        name = self._resolve_index(index)
        kwargs: dict[str, Any] = {
            "index": name,
            "id": doc_id,
        }
        if refresh is not None:
            kwargs["refresh"] = refresh
        result = self.client.delete(**kwargs)
        return self._log_result("delete_doc", result, index=name, doc_id=doc_id)

    def bulk(
        self,
        actions: Iterable[dict[str, Any]],
        *,
        chunk_size: int | None = None,
        refresh: bool = False,
        raise_on_error: bool = False,
    ) -> tuple[int, list[Any]]:
        actual_chunk_size = chunk_size or (
            self.config.bulk_chunk if self.config is not None else 500
        )
        result = self._run_bulk(
            actions,
            chunk_size=actual_chunk_size,
            refresh=refresh,
            raise_on_error=raise_on_error,
        )
        return self._log_result(
            "bulk",
            result,
            chunk_size=actual_chunk_size,
            refresh=refresh,
        )

    def bulk_index(
        self,
        documents: Sequence[Mapping[str, Any]],
        *,
        index: str | None = None,
        id_field: str | None = None,
        chunk_size: int | None = None,
        refresh: bool = False,
        raise_on_error: bool = False,
    ) -> tuple[int, list[Any]]:
        name = self._resolve_index(index)
        actual_chunk_size = chunk_size or (
            self.config.bulk_chunk if self.config is not None else 500
        )

        def iter_actions() -> Iterator[dict[str, Any]]:
            for document in documents:
                source = dict(document)
                action: dict[str, Any] = {"_index": name, "_source": source}
                if id_field and id_field in source:
                    action["_id"] = source[id_field]
                yield action

        result = self._run_bulk(
            iter_actions(),
            chunk_size=actual_chunk_size,
            refresh=refresh,
            raise_on_error=raise_on_error,
        )
        return self._log_result(
            "bulk_index",
            result,
            index=name,
            document_count=len(documents),
            chunk_size=actual_chunk_size,
            refresh=refresh,
        )
