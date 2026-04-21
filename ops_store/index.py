"""Index and alias management services."""

from typing import Any

from .base import OSBase


class OSIndex(OSBase):
    """Class-based index CRUD wrapper around the OpenSearch indices API."""

    def exists(self, index: str | None = None) -> bool:
        name = self._resolve_index(index)
        return self.client.indices.exists(index=name)

    def recreate_index(
        self,
        index: str,
        shards: int = 1,
        replica: int = 0,
        mappings: dict[str, Any] | None = None,
        aliases: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if self.exists(index):
            self.delete(index)
        return self.create(
            index=index,
            mappings=mappings,
            aliases=aliases,
            shards=shards,
            replicas=replica,
        )

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

        return self.client.indices.create(index=name, body=body)

    def delete(self, index: str | None = None) -> dict[str, Any]:
        name = self._resolve_index(index)
        return self.client.indices.delete(index=name)

    def get_settings(self, index: str | None = None) -> dict[str, Any]:
        name = self._resolve_index(index)
        return self.client.indices.get_settings(index=name)

    def get_mapping(self, index: str | None = None) -> dict[str, Any]:
        name = self._resolve_index(index)
        return self.client.indices.get_mapping(index=name)

    def update_settings(
        self,
        settings: dict[str, Any],
        *,
        index: str | None = None,
    ) -> dict[str, Any]:
        name = self._resolve_index(index)
        return self.client.indices.put_settings(
            index=name,
            body={"index": settings},
        )

    def refresh(self, index: str | None = None) -> dict[str, Any]:
        name = self._resolve_index(index)
        return self.client.indices.refresh(index=name)

    def get_aliases(self, index: str | None = None) -> dict[str, Any]:
        name = index or self.default_index
        if name is None:
            return self.client.indices.get_alias()
        return self.client.indices.get_alias(index=name)

    def update_aliases(self, actions: list[dict[str, Any]]) -> dict[str, Any]:
        return self.client.indices.update_aliases(body={"actions": actions})

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

        return self.client.indices.rollover(**kwargs)
