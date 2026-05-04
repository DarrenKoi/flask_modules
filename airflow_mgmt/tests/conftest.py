"""
Shared pytest setup for DAG tests.

Adds the dags/ folder to sys.path so tests can import top-level DAG modules
the same way Airflow's dag-processor does at runtime.
"""

from pathlib import Path
import sys

DAGS_DIR = Path(__file__).resolve().parent.parent / "dags"
if str(DAGS_DIR) not in sys.path:
    sys.path.insert(0, str(DAGS_DIR))
