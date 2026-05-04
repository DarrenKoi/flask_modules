"""
diagnostics / probe_util_import_dag.

Trigger this DAG manually to confirm whether the platform can import the
`util` package after it was relocated from `dags/util/` to `airflow_mgmt/util/`.

The task logs three things to the Airflow task log (Graph → task → Logs):
1. sys.path entries — to see what Python actually searched.
2. Whether `import util` and `import util.minio_handler` succeeded, and
   the resolved `__file__` for each — so you can read the absolute path
   the platform picked up.
3. The traceback if the import fails — easier to triage than the parse-
   time Import Errors banner.
"""

import sys
import traceback
from datetime import datetime

from airflow.sdk import dag, task


@dag(
    dag_id="diagnostics_probe_util_import",
    description="Verify the relocated `util` package is importable on the platform",
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

        for module_name in ("util", "util.orders", "util.minio_handler"):
            print(f"\n# import {module_name}")
            try:
                module = __import__(module_name, fromlist=["__file__"])
            except Exception:
                print("  FAILED")
                traceback.print_exc()
                continue
            print(f"  OK  __file__={getattr(module, '__file__', '<none>')}")

    probe()


probe_util_import()
