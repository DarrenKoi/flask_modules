"""Check whether the Airflow scratch directory is actually writable.

Trigger this DAG manually after deployment. It resolves the scratch
directory the same way the recipe-log helpers do (utils.scratch.scratch_root)
and probes mkdir/write/read/delete against it. If the probe fails, the task
fails red in the Airflow UI.
"""

import getpass
import logging
import os
import platform
import sys
import uuid
from contextlib import suppress
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


@dag(
    dag_id="diagnostics_check_scratch_root",
    description="Check whether SCRATCH_ROOT is writable on the Airflow worker",
    start_date=datetime(2026, 1, 1),
    schedule=None,
    catchup=False,
    max_active_runs=1,
    tags=["diagnostics", "filesystem"],
)
def check_scratch_root():
    @task
    def probe_scratch_root() -> dict:
        target = scratch_root(ROOT_DIR)
        probe_dir = target / ".write_probe" / uuid.uuid4().hex
        fake_data_file = probe_dir / "fake_data.txt"
        fake_payload = (
            "this is fake Airflow SCRATCH_ROOT probe data\n"
            f"probe_id={uuid.uuid4().hex}\n"
        )

        log.info("python executable: %s", sys.executable)
        log.info("python version: %s", sys.version.replace("\n", " "))
        log.info("platform: %s", platform.platform())
        log.info("cwd: %s", Path.cwd())
        uid = os.getuid() if hasattr(os, "getuid") else "<unsupported>"
        gid = os.getgid() if hasattr(os, "getgid") else "<unsupported>"
        log.info("uid/gid: %s/%s", uid, gid)
        log.info("user: %s", getpass.getuser())

        log.info("ROOT_DIR=%s", ROOT_DIR)
        log.info("resolved scratch_root=%s", target)
        log.info("sys.path entries:")
        for item in sys.path:
            log.info("  %s", item)

        try:
            log.info("creating scratch root if missing: %s", target)
            target.mkdir(parents=True, exist_ok=True)
            log.info("creating fake-data probe directory: %s", probe_dir)
            probe_dir.mkdir(parents=True, exist_ok=False)

            log.info("writing fake data file: %s", fake_data_file)
            with fake_data_file.open("w", encoding="utf-8") as fh:
                fh.write(fake_payload)
                fh.flush()
                os.fsync(fh.fileno())

            log.info("reading fake data file back: %s", fake_data_file)
            actual = fake_data_file.read_text(encoding="utf-8")
            if actual != fake_payload:
                raise RuntimeError(
                    f"readback mismatch: expected {fake_payload!r}, got {actual!r}"
                )

            log.info("removing fake data file: %s", fake_data_file)
            fake_data_file.unlink()
            if fake_data_file.exists():
                raise RuntimeError(f"fake data file still exists after unlink: {fake_data_file}")

            log.info("removing fake-data probe directory: %s", probe_dir)
            probe_dir.rmdir()
            if probe_dir.exists():
                raise RuntimeError(f"probe directory still exists after rmdir: {probe_dir}")
        except Exception:
            log.exception("SCRATCH_ROOT probe failed for %s", target)
            raise
        finally:
            with suppress(FileNotFoundError):
                fake_data_file.unlink()
            with suppress(OSError):
                probe_dir.rmdir()
            with suppress(OSError):
                probe_dir.parent.rmdir()

        stat = target.stat()
        result = {
            "scratch_root": str(target),
            "writable": True,
            "fake_write_read_remove_ok": True,
            "mode": oct(stat.st_mode & 0o777),
            "owner_uid": stat.st_uid,
            "owner_gid": stat.st_gid,
        }
        log.info("SCRATCH_ROOT is writable: %s", result)
        return result

    probe_scratch_root()


check_scratch_root()
