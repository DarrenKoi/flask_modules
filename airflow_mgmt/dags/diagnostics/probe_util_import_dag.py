"""
diagnostics / probe_util_import_dag.

Verifies the cross-platform sys.path bootstrap pattern used across this
project: pick a per-OS root_dir and insert it into sys.path so sibling
packages (util, ftp_ingest, ...) resolve as `from util.X import Y`.
"""

import sys
import traceback
from datetime import datetime
from pathlib import Path
from platform import system

from airflow.sdk import dag, task


def _root_dir() -> Path:
    name = system()
    if name == "Windows":
        return Path("F:/skewnono")
    if name == "Linux":
        return Path("/project/workSpace")
    # Darwin / unknown — fall back to this DAG file's local dags folder
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
        print(f"# ROOT_DIR = {ROOT_DIR}  (exists={ROOT_DIR.exists()})")
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
