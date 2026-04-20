"""Index and alias management services."""

from typing import Any

from .base import OSBase


class OSIndex(OSBase):
    """Class-based index CRUD wrapper around the OpenSearch indices API."""

    def exists(self, index: str | None = None) -> bool:
        name = self._resolve_index(index)
        result = self.client.indices.exists(index=name)
        return self._log_result("exists", result, index=name)

    def create(
        self,
        *,
        index: str | None = None,
        mappings: dict[str, Any] | None = None,
        settings: dict[str, Any] | None = None,
        aliases: dict[str, Any] | None = None,
        shards: int = 1,
        replicas: int = 0,
        refresh_interval: str = "30s",
    ) -> dict[str, Any]:
        name = self._resolve_index(index)
        index_settings = dict(settings or {})
        index_settings.setdefault("number_of_shards", shards)
        index_settings.setdefault("number_of_replicas", replicas)
        index_settings.setdefault("refresh_interval", refresh_interval)

        body: dict[str, Any] = {"settings": index_settings}
        if mappings:
            body["mappings"] = mappings
        if aliases:
            body["aliases"] = aliases

        result = self.client.indices.create(index=name, body=body)
        return self._log_result("create_index", result, index=name)

    def delete(self, index: str | None = None) -> dict[str, Any]:
        name = self._resolve_index(index)
        result = self.client.indices.delete(index=name)
        return self._log_result("delete_index", result, index=name)

    def get_settings(self, index: str | None = None) -> dict[str, Any]:
        name = self._resolve_index(index)
        result = self.client.indices.get_settings(index=name)
        return self._log_result("get_settings", result, index=name)

    def get_mapping(self, index: str | None = None) -> dict[str, Any]:
        name = self._resolve_index(index)
        result = self.client.indices.get_mapping(index=name)
        return self._log_result("get_mapping", result, index=name)

    def update_settings(
        self,
        settings: dict[str, Any],
        *,
        index: str | None = None,
    ) -> dict[str, Any]:
        name = self._resolve_index(index)
        result = self.client.indices.put_settings(
            index=name,
            body={"index": settings},
        )
        return self._log_result("update_settings", result, index=name)

    def refresh(self, index: str | None = None) -> dict[str, Any]:
        name = self._resolve_index(index)
        result = self.client.indices.refresh(index=name)
        return self._log_result("refresh_index", result, index=name)

    def get_aliases(self, index: str | None = None) -> dict[str, Any]:
        name = index or self.default_index
        if name is None:
            result = self.client.indices.get_alias()
            return self._log_result("get_aliases", result)
        result = self.client.indices.get_alias(index=name)
        return self._log_result("get_aliases", result, index=name)

    def update_aliases(self, actions: list[dict[str, Any]]) -> dict[str, Any]:
        result = self.client.indices.update_aliases(body={"actions": actions})
        return self._log_result("update_aliases", result, action_count=len(actions))

    def rollover(
        self,
        *,
        alias: str | None = None,
        new_index: str | None = None,
        conditions: dict[str, Any] | None = None,
        settings: dict[str, Any] | None = None,
        mappings: dict[str, Any] | None = None,
        aliases: dict[str, Any] | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        name = self._resolve_index(alias)
        body: dict[str, Any] = {}
        if conditions:
            body["conditions"] = conditions
        if settings:
            body["settings"] = settings
        if mappings:
            body["mappings"] = mappings
        if aliases:
            body["aliases"] = aliases

        params = {"dry_run": True} if dry_run else None
        kwargs: dict[str, Any] = {"alias": name}
        if new_index is not None:
            kwargs["new_index"] = new_index
        if body:
            kwargs["body"] = body
        if params is not None:
            kwargs["params"] = params

        result = self.client.indices.rollover(**kwargs)
        return self._log_result(
            "rollover",
            result,
            alias=name,
            new_index=new_index,
            dry_run=dry_run,
        )
