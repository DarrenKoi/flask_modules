"""Hourly coverage check for the meas_hist_cdsem index.

Used as a scheduler guard. When every closed hour in the lookback window has
at least one document, the calling task logs a noop and exits; when any hour
is empty the task kicks off backfill work.

``has_full_hourly_coverage`` answers the coarse question — does every closed
hour carry *any* document. ``find_missing_fab_hours`` is the per-``fab_name``
breakdown: which of the known fabs produced zero rows in which closed hours.
"""

import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from ops_store import OSSearch, create_client

log = logging.getLogger(__name__)

OPENSEARCH_HOST = "skewnono-db1-os.osp01.skhynix.com"
OPENSEARCH_USER = "skewnono001"
OPENSEARCH_PASSWORD = ""

INDEX = "meas_hist_cdsem"
TIME_FIELD = "timestamp"
TIME_ZONE = "Asia/Seoul"
KST = ZoneInfo(TIME_ZONE)

FAB_FIELD = "fab_name"
KNOWN_FABS: list[str] = [
    # TODO: replace with the real ~10 fab_names before deploy, e.g.
    # "FAB01", "FAB02", ...
]


def _build_search(index: str) -> OSSearch:
    client = create_client(
        host=OPENSEARCH_HOST,
        user=OPENSEARCH_USER,
        password=OPENSEARCH_PASSWORD,
    )
    return OSSearch(client=client, index=index)


def has_full_hourly_coverage(
    *,
    hours: int = 3,
    index: str = INDEX,
    time_field: str = TIME_FIELD,
    search: OSSearch | None = None,
) -> bool:
    """Return True iff each of the last ``hours`` closed hours has >=1 doc.

    Hour buckets align to ``now/h``; the in-progress hour is excluded via the
    ``lt: now/h`` filter. With the default ``min_doc_count=1`` only populated
    hours appear in the aggregation, so coverage holds iff the bucket count
    equals ``hours``.
    """
    svc = search if search is not None else _build_search(index)
    lower = f"now-{hours}h/h"

    result = svc.aggregate(
        {
            "hourly": {
                "date_histogram": {
                    "field": time_field,
                    "calendar_interval": "hour",
                    "time_zone": TIME_ZONE,
                }
            }
        },
        query={
            "range": {
                time_field: {
                    "gte": lower,
                    "lt": "now/h",
                    "time_zone": TIME_ZONE,
                }
            }
        },
        index=index,
    )

    buckets = result.get("aggregations", {}).get("hourly", {}).get("buckets", [])
    populated = len(buckets)

    if populated >= hours:
        log.info(
            "%s coverage complete: %d/%d hours populated", index, populated, hours
        )
        return True

    log.info(
        "%s coverage incomplete: %d/%d hours populated", index, populated, hours
    )
    return False


def _present_fabs_by_hour(
    buckets: list[dict],
    fab_field: str,
) -> dict[datetime, set[str]]:
    """Map each populated KST hour to the set of fabs that carried rows.

    ``buckets`` are the outer ``date_histogram`` buckets; each one's ``key`` is
    epoch UTC milliseconds (the agg's ``time_zone`` only shifts bucket
    *boundaries*, not the meaning of ``key``). The nested ``terms`` sub-agg
    lives under the ``fab_field`` key. Hours absent from ``buckets`` are simply
    not in the returned dict — the caller treats them as fully empty.
    """
    present: dict[datetime, set[str]] = {}
    for bucket in buckets:
        hour = (
            datetime.fromtimestamp(bucket["key"] / 1000, tz=timezone.utc)
            .astimezone(KST)
            .replace(minute=0, second=0, microsecond=0)
        )
        fab_buckets = bucket.get(fab_field, {}).get("buckets", [])
        present[hour] = {fb["key"] for fb in fab_buckets}
    return present


def find_missing_fab_hours(
    *,
    fabs: list[str] | None = None,
    hours: int = 24,
    index: str = INDEX,
    time_field: str = TIME_FIELD,
    fab_field: str = FAB_FIELD,
    now: datetime | None = None,
    search: OSSearch | None = None,
) -> list[dict[str, str]]:
    """Return every ``(fab, closed-hour)`` cell with zero docs as flat records.

    For each of the last ``hours`` closed hours (the in-progress hour is
    excluded via ``lt: now/h``), check each fab in ``fabs`` and emit a record
    ``{"fab_name": ..., "missing_hour": <KST ISO>}`` for every fab that
    produced no document in that hour. ``fabs`` defaults to the module-level
    ``KNOWN_FABS`` — using a *known* list (rather than discovering fabs from the
    index) is deliberate: a fab that is dark for the whole window never appears
    in the aggregation, so only an external list can catch a full outage.

    The hour grid is built in Python (KST) so that a fully-empty hour — which
    yields no aggregation bucket at all — still reports a record for every fab.
    ``now`` is injectable so the grid is deterministic under test; in a live run
    it defaults to ``datetime.now(KST)`` and must track the cluster clock to
    within the boundary hour, matching the server-side ``now/h`` window.
    """
    fab_list = fabs if fabs is not None else KNOWN_FABS
    if not fab_list:
        log.warning("no fabs configured (KNOWN_FABS empty, no override); returning []")
        return []

    svc = search if search is not None else _build_search(index)
    lower = f"now-{hours}h/h"

    result = svc.aggregate(
        {
            "hourly": {
                "date_histogram": {
                    "field": time_field,
                    "calendar_interval": "hour",
                    "time_zone": TIME_ZONE,
                    "min_doc_count": 0,
                },
                "aggs": {
                    fab_field: {"terms": {"field": fab_field, "size": 50}},
                },
            }
        },
        query={
            "range": {
                time_field: {
                    "gte": lower,
                    "lt": "now/h",
                    "time_zone": TIME_ZONE,
                }
            }
        },
        index=index,
    )

    buckets = result.get("aggregations", {}).get("hourly", {}).get("buckets", [])
    present_by_hour = _present_fabs_by_hour(buckets, fab_field)

    top = (now or datetime.now(KST)).astimezone(KST).replace(
        minute=0, second=0, microsecond=0
    )
    grid = [top - timedelta(hours=h) for h in range(hours, 0, -1)]

    records: list[dict[str, str]] = []
    for hour in grid:
        present = present_by_hour.get(hour, set())
        for fab in fab_list:
            if fab not in present:
                records.append({"fab_name": fab, "missing_hour": hour.isoformat()})

    records.sort(key=lambda r: (r["missing_hour"], r["fab_name"]))
    log.info(
        "%s fab gaps: %d missing (fab, hour) cells over %d closed hours",
        index,
        len(records),
        hours,
    )
    return records
