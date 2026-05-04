"""
Shared pytest setup for DAG tests.

Adds the airflow_mgmt/ root to sys.path so tests can import top-level
packages (utils, minio_handler) the same way the platform's worker
does after the DAG-side _root_dir bootstrap runs.
"""

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent  # airflow_mgmt/
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
