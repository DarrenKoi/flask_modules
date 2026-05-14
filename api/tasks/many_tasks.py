"""Individual task implementations. Wrapped with @redis_lock + @with_logging in schedule.py."""

import logging
import re
import time
from datetime import date
from pathlib import Path

log = logging.getLogger(__name__)

UWSGI_RESTART_PATH = Path("/project/workSpace/restart.txt")
LOGS_DIR = Path("/project/workSpace/logs")
# Matches active log (uwsgi-YYYY-MM-DD.log) and rolled-over files
# (uwsgi-YYYY-MM-DD.log.<epoch>). Group 1 = log date, group 2 = rotation epoch or None.
LOG_FILENAME_RE = re.compile(r"^uwsgi-(\d{4}-\d{2}-\d{2})\.log(?:\.(\d+))?$")
KEEP_LOG_COUNT = 7


def task1() -> None:
    log.info("task1 start")
    time.sleep(1)
    log.info("task1 done")


def task2() -> None:
    log.info("task2 start")
    time.sleep(1)
    log.info("task2 done")


def restart_uwsgi() -> None:
    """Touch the uWSGI reload file. Runs daily at 1 AM (see JOB_FUNCTIONS).

    Updating the file's mtime triggers `touch-reload` in wsgi.ini. Memory
    refresh + side-effects from cloud host maintenance are the motivation.
    """
    UWSGI_RESTART_PATH.touch()
    log.info("touched %s for uwsgi reload", UWSGI_RESTART_PATH)


def purge_old_logs() -> None:
    """Keep the newest KEEP_LOG_COUNT uwsgi log files, delete the rest.

    Ordering is by log date in the filename, then by rotation epoch suffix.
    The active log (no suffix) sorts as newest within its date, so it's
    always retained when present.
    """
    candidates: list[tuple[date, float, Path]] = []
    for f in LOGS_DIR.iterdir():
        m = LOG_FILENAME_RE.match(f.name)
        if not m:
            continue
        log_date = date.fromisoformat(m.group(1))
        epoch = float(m.group(2)) if m.group(2) else float("inf")
        candidates.append((log_date, epoch, f))

    candidates.sort(key=lambda c: (c[0], c[1]), reverse=True)
    removed = 0
    for _, _, f in candidates[KEEP_LOG_COUNT:]:
        f.unlink()
        removed += 1
        log.info("removed old log %s", f)
    log.info(
        "purge_old_logs removed=%d kept=%d",
        removed,
        min(len(candidates), KEEP_LOG_COUNT),
    )
