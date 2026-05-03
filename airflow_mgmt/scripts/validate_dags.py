"""
Lightweight DAG validator — no pytest, no Airflow scheduler.

Usage:
    python scripts/validate_dags.py

Useful as a pre-commit hook or sanity check before pushing to Bitbucket.
Exits non-zero if any DAG fails to import.
"""

import sys
from pathlib import Path

from airflow.models import DagBag

DAGS_DIR = Path(__file__).resolve().parent.parent / "dags"


def main() -> int:
    bag = DagBag(dag_folder=str(DAGS_DIR), include_examples=False)

    if bag.import_errors:
        print("Import errors found:")
        for path, err in bag.import_errors.items():
            print(f"  {path}")
            print(f"    {err}")
        return 1

    print(f"OK — discovered {len(bag.dags)} DAG(s):")
    for dag_id in sorted(bag.dags):
        dag = bag.dags[dag_id]
        print(f"  - {dag_id}  (tasks: {len(dag.tasks)}, schedule: {dag.schedule_interval})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
