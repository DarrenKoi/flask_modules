"""
diagnostics / inspect_packages_dag.

Trigger this DAG manually to list Python packages installed on the
Airflow worker. Useful for "is package X already available?" questions
without needing shell access to the cluster.

The task prints results to its log so they show up in the Airflow UI
(Graph → task → Logs).
"""

import logging
from datetime import datetime
from importlib.metadata import distributions

from airflow.providers.standard.operators.python import PythonOperator
from airflow.sdk import DAG

log = logging.getLogger(__name__)


def list_packages() -> None:
    packages = sorted(
        ((dist.metadata["Name"] or "<unknown>").strip(), dist.version)
        for dist in distributions()
    )
    log.info("%d package(s) installed:", len(packages))
    for name, version in packages:
        log.info("  %s==%s", name, version)


with DAG(
    dag_id="diagnostics_inspect_packages",
    description="List Python packages installed on the Airflow worker",
    start_date=datetime(2026, 1, 1),
    schedule=None,
    catchup=False,
    tags=["diagnostics"],
) as dag:
    PythonOperator(
        task_id="list_packages",
        python_callable=list_packages,
    )
