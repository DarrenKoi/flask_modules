"""Hourly coverage check for the meas_hist_cdsem index.

Used as a scheduler guard. When every closed hour in the lookback window has
at least one document, the calling task logs a noop and exits; when any hour
is empty the task kicks off backfill work.
"""

import logging

from ops_store import OSSearch

log = logging.getLogger(__name__)

INDEX = "meas_hist_cdsem"
TIME_FIELD = "timestamp"


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
    svc = search if search is not None else OSSearch(index=index)
    lower = f"now-{hours}h/h"

    result = svc.aggregate(
        {
            "hourly": {
                "date_histogram": {
                    "field": time_field,
                    "calendar_interval": "hour",
                }
            }
        },
        query={"range": {time_field: {"gte": lower, "lt": "now/h"}}},
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
