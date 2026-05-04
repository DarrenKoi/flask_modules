"""
Shared pytest setup for DAG tests.

Mirrors the sys.path bootstrap that DAG files do at parse time, so tests
can import top-level packages (utils, minio_handler) the same way the
Airflow worker does. Also exports AIRFLOW_MGMT_ROOT for any code that
prefers the env var over its own auto-detect.
"""

import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent  # airflow_mgmt/
os.environ.setdefault("AIRFLOW_MGMT_ROOT", str(ROOT))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
