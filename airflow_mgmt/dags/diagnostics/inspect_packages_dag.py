"""
diagnostics / inspect_packages_dag.

Trigger this DAG manually to list Python packages installed on the
Airflow worker. Useful for "is package X already available?" questions
without needing shell access to the cluster.

The task prints results to its log so they show up in the Airflow UI
(Graph → task → Logs).
"""

from datetime import datetime
from importlib.metadata import distributions

from airflow.sdk import dag, task


@dag(
    dag_id="diagnostics_inspect_packages",
    description="List Python packages installed on the Airflow worker",
    start_date=datetime(2026, 1, 1),
    schedule=None,
    catchup=False,
    tags=["diagnostics"],
)
def inspect_packages():
    @task
    def list_packages() -> None:
        packages = sorted(
            ((dist.metadata["Name"] or "<unknown>").strip(), dist.version)
            for dist in distributions()
        )
        print(f"# {len(packages)} package(s) installed:")
        for name, version in packages:
            print(f"  {name}=={version}")

    list_packages()


inspect_packages()
