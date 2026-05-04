"""
diagnostics / probe_util_import_dag.

Confirms the fix for the company-platform import problem:

The platform git-registers DAGs into a nested path like
    /opt/airflow/dags/airflow_repo.git/<scheduler>/dags/...
but only `/opt/airflow/dags` is on sys.path. So `from util.X import Y`
fails even though `dags/util/` exists on disk — it's just not on the
import path.

Fix: each DAG file inserts its own parent (the local `dags/` folder)
into sys.path before importing siblings.

This probe runs the imports twice — once before the bootstrap (expect
ModuleNotFoundError) and once after (expect success) — to prove the
pattern works on this platform.
"""

import os
import sys
import traceback
from datetime import datetime
from pathlib import Path

from airflow.sdk import dag, task


def _try_imports(label: str) -> None:
    print(f"\n# Imports — {label}")
    for module_name in ("util", "util.orders", "util.minio_handler"):
        print(f"  import {module_name}")
        try:
            module = __import__(module_name, fromlist=["__file__"])
        except Exception:
            print("    FAILED")
            traceback.print_exc()
            continue
        print(f"    OK  __file__={getattr(module, '__file__', '<none>')}")


@dag(
    dag_id="diagnostics_probe_util_import",
    description="Confirm the sys.path bootstrap pattern fixes sibling-package imports",
    start_date=datetime(2026, 1, 1),
    schedule=None,
    catchup=False,
    tags=["diagnostics"],
)
def probe_util_import():
    @task
    def probe() -> None:
        print("# Initial sys.path:")
        for entry in sys.path:
            print(f"  {entry}")

        # Drop any cached import of `util` so the second attempt is honest.
        for cached in [m for m in sys.modules if m == "util" or m.startswith("util.")]:
            del sys.modules[cached]

        _try_imports("BEFORE bootstrap (expected to fail)")

        local_dags = Path(__file__).resolve().parents[1]
        print(f"\n# Bootstrapping sys.path with local dags folder:\n  {local_dags}")
        if str(local_dags) not in sys.path:
            sys.path.insert(0, str(local_dags))

        for cached in [m for m in sys.modules if m == "util" or m.startswith("util.")]:
            del sys.modules[cached]

        _try_imports("AFTER bootstrap (expected to succeed)")

    probe()


probe_util_import()
