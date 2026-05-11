"""Individual task implementations. Wrapped with @redis_lock + @with_logging in schedule.py."""

import logging
import re
import time
from datetime import date, timedelta
from pathlib import Path

log = logging.getLogger(__name__)

UWSGI_RESTART_PATH = Path("/project/workSpace/restart.txt")
LOGS_DIR = Path("/project/workSpace/logs")
# Matches the daemonize macro output: uwsgi-YYYY-MM-DD.log
LOG_FILENAME_RE = re.compile(r"^uwsgi-(\d{4}-\d{2}-\d{2})\.log$")


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
    """Delete uwsgi-YYYY-MM-DD.log files older than 7 days.

    Reads the date from the filename, not from mtime — the file keeps being
    appended to all day, so mtime is one day ahead of the canonical date.
    Schedule this AFTER restart_uwsgi so today's file already exists when
    we run; today's filename is never < cutoff so the active log is safe.
    """
    cutoff = date.today() - timedelta(days=7)
    removed = 0
    for f in LOGS_DIR.iterdir():
        m = LOG_FILENAME_RE.match(f.name)
        if m and date.fromisoformat(m.group(1)) < cutoff:
            f.unlink()
            removed += 1
            log.info("removed old log %s", f)
    log.info("purge_old_logs removed=%d cutoff=%s", removed, cutoff)
