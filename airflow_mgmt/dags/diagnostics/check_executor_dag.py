"""Probe what the Airflow executor actually does to filesystem state.

Two tasks run sequentially. Task 1 writes a marker file under scratch_root
and logs hostname + configured executor. Task 2 tries to read the marker
back and logs its own hostname.

Interpret the result:

  - Same hostname, file present  → LocalExecutor (or single worker).
                                   Filesystem-as-state-store between tasks
                                   is safe.

  - Different hostname, file present → Distributed executor (Celery / K8s)
                                       with a shared volume mount. Still
                                       safe.

  - File missing (FileNotFoundError) → Distributed executor, no shared
                                       volume. Each task lands on its own
                                       filesystem. Cleanup-at-end-of-DAG
                                       won't work — keep cleanup in-process
                                       with the download, OR ask ops for a
                                       shared volume.

Trigger manually from the Airflow UI. Cleanup runs in task 2's `finally`.
"""

import logging
import socket
import sys
from datetime import datetime
from pathlib import Path

from airflow.sdk import dag, task

log = logging.getLogger(__name__)


# ── sys.path bootstrap ──────────────────────────────────────────────────────
def _find_root(marker: str = "project_root.txt") -> Path:
    try:
        start = Path(__file__).resolve().parent
    except NameError:  # REPL / python -c / exec()
        start = Path.cwd().resolve()
    for p in (start, *start.parents):
        if (p / marker).is_file():
            return p
    raise RuntimeError(f"{marker!r} not found above {start}")


ROOT_DIR = _find_root()
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))
# ────────────────────────────────────────────────────────────────────────────

from utils.scratch import scratch_root  # noqa: E402


def _marker_path(run_id: str) -> Path:
    return scratch_root(ROOT_DIR) / "check_executor" / run_id / "marker.txt"


@dag(
    dag_id="diagnostics_check_executor",
    description="Verify whether tasks share filesystem state across the executor",
    start_date=datetime(2026, 1, 1),
    schedule=None,
    catchup=False,
    max_active_runs=1,
    tags=["diagnostics", "executor"],
)
def check_executor():

    @task
    def write_marker(**ctx) -> None:
        from airflow.configuration import conf

        marker = _marker_path(ctx["dag_run"].run_id)
        marker.parent.mkdir(parents=True, exist_ok=True)
        payload = f"hostname={socket.gethostname()}\nrun_id={ctx['dag_run'].run_id}\n"
        marker.write_text(payload, encoding="utf-8")

        log.info("wrote marker: %s", marker)
        log.info("hostname: %s", socket.gethostname())
        log.info("configured executor: %s", conf.get("core", "executor"))

    @task
    def read_marker(**ctx) -> None:
        marker = _marker_path(ctx["dag_run"].run_id)
        log.info("hostname: %s", socket.gethostname())
        log.info("looking for marker: %s", marker)

        try:
            content = marker.read_text(encoding="utf-8")
            log.info("MARKER FOUND — filesystem is shared between tasks")
            log.info("marker contents:\n%s", content)
        except FileNotFoundError:
            log.error(
                "MARKER MISSING — tasks do NOT share a filesystem. "
                "Cleanup-at-end-of-DAG will not work. Keep cleanup in-process "
                "with the download, or ask ops for a shared volume mount."
            )
            raise
        finally:
            from contextlib import suppress
            with suppress(FileNotFoundError):
                marker.unlink()
            with suppress(OSError):
                marker.parent.rmdir()

    write_marker() >> read_marker()


check_executor()
