"""Click event logging and aggregations, layered on top of ``ops_store``."""

import os
from datetime import datetime, timezone
from typing import Any

from ops_store import OSDoc

DEFAULT_INDEX = "url_shortner_clicks"


class ClickAnalytics:
    """Record short-code clicks to OpenSearch and answer simple aggregations.

    Composes an :class:`ops_store.OSDoc` rather than building its own client,
    so this package reuses the connection logic already exercised by the
    rest of the repo.
    """

    def __init__(
        self,
        doc_service: OSDoc | None = None,
        *,
        index: str | None = None,
    ) -> None:
        self.index = index or os.getenv("URLSHORTNER_ANALYTICS_INDEX", DEFAULT_INDEX)
        self.doc_service = doc_service if doc_service is not None else OSDoc(index=self.index)

    def log_click(
        self,
        code: str,
        *,
        timestamp: datetime | None = None,
        ip: str | None = None,
        user_agent: str | None = None,
        referrer: str | None = None,
        owner: str | None = None,
    ) -> None:
        event = {
            "code": code,
            "timestamp": (timestamp or datetime.now(timezone.utc)).isoformat(),
            "ip": ip,
            "user_agent": user_agent,
            "referrer": referrer,
            "owner": owner,
        }
        self.doc_service.index(event, index=self.index)

    def top_codes(self, *, window: str = "7d", size: int = 10) -> list[dict[str, Any]]:
        """Return the most-clicked codes within the given time window."""

        body = {
            "size": 0,
            "query": {
                "range": {
                    "timestamp": {"gte": f"now-{window}"},
                }
            },
            "aggs": {
                "top_codes": {
                    "terms": {"field": "code", "size": size},
                }
            },
        }
        response = self.doc_service.client.search(index=self.index, body=body)
        buckets = response.get("aggregations", {}).get("top_codes", {}).get("buckets", [])
        return [{"code": b["key"], "clicks": b["doc_count"]} for b in buckets]
