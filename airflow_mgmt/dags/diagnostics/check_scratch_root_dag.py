"""Check whether the Airflow scratch directory is actually writable.

Trigger this DAG manually after deployment. It resolves the same scratch
directory convention used by the recipe-log helpers:

1. AIRFLOW_MGMT_SCRATCH_ROOT, if set.
2. SCRATCH_ROOT, if set as a shorter local override.
3. /tmp/airflow_mgmt on Airflow workers.

The task logs the resolved path, cwd, user, and sys.path, then performs a real
mkdir/write/read/delete probe. If the probe fails, the task fails red in the
Airflow UI.
"""

import getpass
import logging
import os
import platform
import sys
import tempfile
import uuid
from contextlib import suppress
from datetime import datetime
from pathlib import Path

from airflow.sdk import dag, task

log = logging.getLogger(__name__)


def _resolve_scratch_root() -> Path:
    env = os.getenv("AIRFLOW_MGMT_SCRATCH_ROOT") or os.getenv("SCRATCH_ROOT")
    if env:
        return Path(env).expanduser().resolve()
    return Path(tempfile.gettempdir()).resolve() / "airflow_mgmt"


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
        scratch_root = _resolve_scratch_root()
        probe_dir = scratch_root / ".write_probe" / uuid.uuid4().hex
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

        log.info("AIRFLOW_MGMT_SCRATCH_ROOT=%r", os.getenv("AIRFLOW_MGMT_SCRATCH_ROOT"))
        log.info("SCRATCH_ROOT=%r", os.getenv("SCRATCH_ROOT"))
        log.info("resolved scratch_root=%s", scratch_root)
        log.info("sys.path entries:")
        for item in sys.path:
            log.info("  %s", item)

        try:
            log.info("creating scratch root if missing: %s", scratch_root)
            scratch_root.mkdir(parents=True, exist_ok=True)
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
            log.exception("SCRATCH_ROOT probe failed for %s", scratch_root)
            raise
        finally:
            with suppress(FileNotFoundError):
                fake_data_file.unlink()
            with suppress(OSError):
                probe_dir.rmdir()
            with suppress(OSError):
                probe_dir.parent.rmdir()

        stat = scratch_root.stat()
        result = {
            "scratch_root": str(scratch_root),
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
