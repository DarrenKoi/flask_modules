"""
Shared pytest setup for DAG tests.

Mirrors the sys.path bootstrap that DAG files do at parse time, so tests
can import top-level packages (minio_handler) the same way the Airflow
worker does.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent  # airflow_mgmt/
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
