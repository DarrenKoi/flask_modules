"""
Shared pytest setup for DAG tests.

Adds the dags/ folder and the airflow_mgmt/ root to sys.path so tests can
import top-level DAG modules and the sibling `util` package. On the
platform, the equivalent of `airflow_mgmt/` must also be on sys.path for
`from util.X import Y` to resolve.
"""

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
DAGS_DIR = ROOT / "dags"

for path in (ROOT, DAGS_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))
