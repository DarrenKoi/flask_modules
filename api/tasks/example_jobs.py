"""Stand-in tasks for the scheduling patterns registered in ``api/schedule.py``.

Each function here is a no-op that logs; the point is the JOB_FUNCTIONS entry
that schedules it, not the body. The docstrings record the runtime each one
stands in for, because runtime is what drives the trigger, the slot and the
misfire grace on the registry side.

Nothing sleeps. A real 20-minute job would hold an executor thread for 20
minutes, which is exactly the pressure these entries are shaped around — but
making the mocks actually block would only make the dashboard slow to watch.

Swap the bodies for real work and the surrounding config still applies.
"""

import logging

log = logging.getLogger(__name__)


def hourly_extract() -> None:
    """Stands in for a 15-20 min pull: wide window, heavy, must not be lost."""
    log.info("hourly_extract: pretending to pull an hour of source rows")


def halfhour_sync() -> None:
    """Stands in for a 5-10 min incremental sync on a 30 min cadence."""
    log.info("halfhour_sync: pretending to sync the last 30 minutes")


def freshness_probe() -> None:
    """Stands in for a sub-second check. Worthless if it runs late."""
    log.info("freshness_probe: pretending to check index recency")


def daily_rollup() -> None:
    """Stands in for a 20 min nightly aggregate."""
    log.info("daily_rollup: pretending to roll up yesterday")


def intraday_refresh() -> None:
    """Stands in for a 5 min refresh that only matters during work hours."""
    log.info("intraday_refresh: pretending to refresh the working set")


def weekly_compaction() -> None:
    """Stands in for a 20+ min weekend maintenance pass."""
    log.info("weekly_compaction: pretending to compact old partitions")
