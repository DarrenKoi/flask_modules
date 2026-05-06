"""MongoDB-backed URL mapping store."""

from datetime import datetime, timezone
from typing import Any

from .base import MongoBase


class URLMapping(MongoBase):
    """Persist and retrieve ``code -> long URL`` mappings in MongoDB."""

    def create(
        self,
        code: str,
        url: str,
        *,
        owner: str | None = None,
        is_custom: bool = False,
        database: str | None = None,
        collection: str | None = None,
    ) -> dict[str, Any]:
        document: dict[str, Any] = {
            "_id": code,
            "url": url,
            "owner": owner,
            "is_custom": is_custom,
            "created_at": datetime.now(timezone.utc),
        }
        coll = self._coll(database=database, collection=collection)
        coll.insert_one(document)
        return document

    def lookup(
        self,
        code: str,
        *,
        database: str | None = None,
        collection: str | None = None,
    ) -> dict[str, Any] | None:
        coll = self._coll(database=database, collection=collection)
        return coll.find_one({"_id": code})

    def list_by_owner(
        self,
        owner: str,
        *,
        limit: int = 100,
        database: str | None = None,
        collection: str | None = None,
    ) -> list[dict[str, Any]]:
        coll = self._coll(database=database, collection=collection)
        cursor = coll.find({"owner": owner}).sort("created_at", -1).limit(limit)
        return list(cursor)

    def ensure_indexes(
        self,
        *,
        database: str | None = None,
        collection: str | None = None,
    ) -> None:
        """Create the secondary indexes used for owner lookups.

        The unique index on ``_id`` is implicit in MongoDB. This method only
        creates extra indexes such as ``owner`` for the per-employee listing
        view.
        """

        coll = self._coll(database=database, collection=collection)
        coll.create_index("owner")
        coll.create_index("created_at")
