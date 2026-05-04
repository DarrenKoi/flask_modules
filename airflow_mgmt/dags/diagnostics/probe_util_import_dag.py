"""
diagnostics / probe_util_import_dag.

Verifies the cross-platform sys.path bootstrap pattern: pick a per-host
root_dir and insert it into sys.path so sibling packages
(util, ftp_ingest, ...) resolve as `from util.X import Y`.

Hosts:
- Airflow worker  → /opt/airflow/dags/airflow_repo.git/skewnono-scheduler1/dags
- Windows dev     → F:/skewnono
- Linux dev       → /project/workSpace
- Other / Darwin  → derived from __file__

Airflow is detected by checking whether this file's resolved path
contains "/opt/airflow/" — the platform deploys clones under that root.
"""

import sys
import traceback
from datetime import datetime
from pathlib import Path
from platform import system

from airflow.sdk import dag, task


def _root_dir() -> Path:
    if "/opt/airflow/" in str(Path(__file__).resolve()):
        return Path("/opt/airflow/dags/airflow_repo.git/skewnono-scheduler1/dags")
    name = system()
    if name == "Windows":
        return Path("F:/skewnono")
    if name == "Linux":
        return Path("/project/workSpace")
    return Path(__file__).resolve().parents[1]


ROOT_DIR = _root_dir()
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))


@dag(
    dag_id="diagnostics_probe_util_import",
    description="Verify cross-platform sys.path bootstrap exposes vendored packages",
    start_date=datetime(2026, 1, 1),
    schedule=None,
    catchup=False,
    tags=["diagnostics"],
)
def probe_util_import():
    @task
    def probe() -> None:
        print(f"# platform.system() = {system()!r}")
        print(f"# __file__          = {__file__}")
        print(f"# ROOT_DIR          = {ROOT_DIR}  (exists={ROOT_DIR.exists()})")

        if ROOT_DIR.exists():
            print("# Top-level entries in ROOT_DIR:")
            for child in sorted(ROOT_DIR.iterdir()):
                kind = "DIR " if child.is_dir() else "FILE"
                print(f"  {kind} {child.name}")

        print("\n# sys.path:")
        for entry in sys.path:
            print(f"  {entry}")

        print("\n# Imports:")
        for name in ("util", "util.orders", "util.minio_handler"):
            print(f"  import {name}")
            try:
                module = __import__(name, fromlist=["__file__"])
            except Exception:
                print("    FAILED")
                traceback.print_exc()
                continue
            print(f"    OK  __file__={getattr(module, '__file__', '<none>')}")

    probe()


probe_util_import()
