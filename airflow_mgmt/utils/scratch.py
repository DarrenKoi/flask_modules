"""Resolve a writable scratch directory.

ROOT_DIR is the read-only code mount on Airflow workers; scratch is the
writable runtime dir. On Airflow we write under /tmp/<root_dir.name>
(the worker is Linux, so /tmp is hardcoded — no tempfile needed); locally
we use root_dir/scratch so files stay inspectable in the IDE.
Worker detection: any sys.path entry contains "/opt/airflow".
"""

import sys
from pathlib import Path


def scratch_root(root_dir: Path) -> Path:
    if any("/opt/airflow" in p for p in sys.path):
        return Path("/tmp") / root_dir.name
    return root_dir / "scratch"
