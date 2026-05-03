"""
Example 01 — The simplest possible Airflow 3.x DAG.

Demonstrates:
- @dag decorator from the new airflow.sdk namespace
- @task decorator (TaskFlow API)
- A single task with a return value (auto-pushed to XCom)

In Airflow 3.x, all DAG-authoring APIs live under `airflow.sdk`.
This is the stable interface — internal modules like `airflow.models.dag.DAG`
are deprecated and will be removed.
"""

from datetime import datetime

from airflow.sdk import dag, task


@dag(
    dag_id="example_01_hello_world",
    start_date=datetime(2026, 1, 1),
    schedule=None,            # manual trigger only — good for learning
    catchup=False,            # don't backfill missed runs
    tags=["example", "learning"],
)
def hello_world_dag():
    @task
    def say_hello() -> str:
        message = "Hello from Airflow 3.1!"
        print(message)
        return message

    say_hello()


hello_world_dag()
