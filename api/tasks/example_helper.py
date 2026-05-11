"""Example helper module imported by tasks.

The pattern for any separate file that a task imports:

  1. ``log = logging.getLogger(__name__)`` at module top.
  2. Use ``log.info`` / ``log.warning`` / ``log.exception`` inside functions.
  3. Do NOT call ``logging.basicConfig()``, do NOT add handlers, do NOT set
     levels. The Flask app / uWSGI master configures the root logger once;
     records from this module propagate up through the
     ``api.tasks.example_helper`` -> ``api.tasks`` -> ``api`` -> root chain.

To watch these logs:

  * ``flask run`` -> records appear on stderr.
  * Under uWSGI -> records go wherever ``wsgi.ini`` sends stderr (a file,
    a syslog target, etc.). No code change needed in this module.

This is stdlib ``logging``. It is independent of :class:`TaskLogger`
(``api/extension.py``), which records start/end/error events to Redis for
the ``/jobs/logs`` dashboard. Use both: ``TaskLogger`` wraps the outer
task to publish run state; ``log.info`` inside helpers gives you the
detailed breadcrumb trail.
"""

import logging
from pathlib import Path

log = logging.getLogger(__name__)


def count_files(directory: Path, suffix: str) -> int:
    """Return the number of files in ``directory`` matching ``*<suffix>``.

    Demonstrates the three log levels you'll reach for most often:

    * ``debug`` — verbose, off in production by default.
    * ``info`` — normal progress / outcomes.
    * ``warning`` — recoverable anomaly the caller may want to know about.
    """
    log.debug("scanning %s for *%s", directory, suffix)
    if not directory.exists():
        log.warning("directory does not exist: %s", directory)
        return 0

    matches = list(directory.glob(f"*{suffix}"))
    log.info("found %d %s file(s) in %s", len(matches), suffix, directory)
    return len(matches)


def process_payload(payload: dict) -> dict:
    """Demonstrate ``log.exception`` — captures the active traceback.

    Use ``log.exception`` (not ``log.error``) inside an ``except`` block.
    It emits at ERROR level and attaches the exception's stack frames,
    which is what you actually want in production logs.
    """
    try:
        result = {"id": payload["id"], "value": int(payload["value"])}
    except (KeyError, ValueError, TypeError):
        # ``log.exception`` only makes sense inside ``except``; outside of
        # one, the traceback it attaches will be empty.
        log.exception("invalid payload: %r", payload)
        raise

    log.info("processed payload id=%s", result["id"])
    return result
