"""Individual task implementations. Wrapped with @redis_lock + @with_logging in schedule.py."""

import logging
import os
import time
from pathlib import Path

log = logging.getLogger(__name__)


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

    Updating the file's mtime triggers `touch-reload` in uwsgi.ini. Memory
    refresh + side-effects from cloud host maintenance are the motivation.
    """
    path = Path(os.getenv("UWSGI_RESTART_PATH", "/project/workSpace/restart.txt"))
    path.touch()
    log.info("touched %s for uwsgi reload", path)
