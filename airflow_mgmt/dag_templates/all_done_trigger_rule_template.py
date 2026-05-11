"""
template / all_done_trigger_rule_template.

Example of a classic `with DAG(...)` workflow where later tasks keep running
even when earlier upstream tasks fail.

Use this when:
  - You need cleanup, notification, audit, or final status tasks to run after
    upstream tasks finish, regardless of success or failure.
  - You prefer explicit operator objects instead of TaskFlow decorators.
  - You want to understand where `trigger_rule="all_done"` belongs.

Important:
  - Airflow's default trigger rule is `all_success`.
  - Set `trigger_rule="all_done"` on each downstream task that must run after
    upstream failure.
  - `all_done` only controls scheduling. If a failed upstream task did not
    produce an XCom value, downstream code must handle that missing value.

This file lives OUTSIDE airflow_mgmt/dags/ so Airflow does not auto-load
it. Copy into dags/<topic>/ and rename when you adapt it.
"""

import logging
from datetime import datetime

from airflow.providers.standard.operators.python import PythonOperator
from airflow.sdk import DAG

log = logging.getLogger(__name__)


def _extract() -> dict:
    log.info("extract started")
    return {"batch_id": "example-001", "rows": 100}


def _load(**context) -> None:
    ti = context["ti"]
    extract_result = ti.xcom_pull(task_ids="extract")
    log.info("load received extract result: %s", extract_result)

    # Simulate a load failure. In a real DAG this might be a database, MinIO,
    # OpenSearch, or external API failure.
    raise RuntimeError("example load failure")


def _audit_after_load(**context) -> None:
    ti = context["ti"]
    extract_result = ti.xcom_pull(task_ids="extract")
    load_result = ti.xcom_pull(task_ids="load")

    log.info("audit still runs after load failure")
    log.info("extract_result=%s", extract_result)
    log.info("load_result=%s", load_result)


def _cleanup() -> None:
    log.info("cleanup runs whether upstream tasks succeeded or failed")


def _final_report() -> None:
    log.info("final report runs after cleanup is done")


with DAG(
    dag_id="template_all_done_trigger_rule",
    description="Template: keep downstream PythonOperator tasks running after upstream failure",
    start_date=datetime(2026, 1, 1),
    schedule=None,
    catchup=False,
    tags=["template", "with-dag", "trigger-rule"],
) as dag:
    extract = PythonOperator(
        task_id="extract",
        python_callable=_extract,
    )

    load = PythonOperator(
        task_id="load",
        python_callable=_load,
    )

    audit_after_load = PythonOperator(
        task_id="audit_after_load",
        python_callable=_audit_after_load,
        trigger_rule="all_done",
    )

    cleanup = PythonOperator(
        task_id="cleanup",
        python_callable=_cleanup,
        trigger_rule="all_done",
    )

    final_report = PythonOperator(
        task_id="final_report",
        python_callable=_final_report,
        trigger_rule="all_done",
    )

    extract >> load >> audit_after_load >> cleanup >> final_report
