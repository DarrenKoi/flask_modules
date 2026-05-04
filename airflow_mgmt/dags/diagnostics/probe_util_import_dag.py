"""
diagnostics / probe_util_import_dag.

Trigger this DAG manually to confirm the platform actually ships the
vendored `util/` subfolder under `dags/` and that the imports resolve.

The task logs four things to the Airflow task log (Graph → task → Logs):
1. sys.path entries.
2. For each sys.path entry that looks like a dags folder, the *physical*
   contents — files and subfolders. This reveals whether the platform
   git-register includes nested packages or only top-level .py files.
3. The same listing for any candidate util location found.
4. Whether `import util`, `import util.orders`, and
   `import util.minio_handler` succeeded, with __file__ on success and
   the traceback on failure.
"""

import os
import sys
import traceback
from datetime import datetime
from pathlib import Path

from airflow.sdk import dag, task


@dag(
    dag_id="diagnostics_probe_util_import",
    description="Verify dags/util/ ships and resolves on the platform",
    start_date=datetime(2026, 1, 1),
    schedule=None,
    catchup=False,
    tags=["diagnostics"],
)
def probe_util_import():
    @task
    def probe() -> None:
        print("# sys.path entries:")
        for entry in sys.path:
            print(f"  {entry}")

        print("\n# Contents of dags-like sys.path entries:")
        for entry in sys.path:
            if "dags" not in entry.lower():
                continue
            path = Path(entry)
            if not path.exists():
                print(f"  {entry}  (MISSING on disk)")
                continue
            print(f"  {entry}  →")
            try:
                for child in sorted(path.iterdir()):
                    kind = "DIR " if child.is_dir() else "FILE"
                    print(f"    {kind} {child.name}")
            except Exception as exc:
                print(f"    listing failed: {exc!r}")

        print("\n# This DAG file's location (__file__):")
        print(f"  {__file__}")
        print("# Walking up from this DAG to find util/:")
        for parent in Path(__file__).resolve().parents[:5]:
            candidate = parent / "util"
            marker = "FOUND" if candidate.exists() else "missing"
            print(f"  {marker:7} {candidate}")

        print("\n# Imports:")
        for module_name in ("util", "util.orders", "util.minio_handler"):
            print(f"\n  import {module_name}")
            try:
                module = __import__(module_name, fromlist=["__file__"])
            except Exception:
                print("    FAILED")
                traceback.print_exc()
                continue
            print(f"    OK  __file__={getattr(module, '__file__', '<none>')}")

    probe()


probe_util_import()
