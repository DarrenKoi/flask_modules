"""
Example 04 — Conditional branching with @task.branch.

Demonstrates:
- Choosing which downstream task runs based on a condition
- The @task.branch decorator returning a task_id (or list) to follow
- Trigger rules so the converging task runs even when only one branch ran

Real-world use: skip an expensive step on weekends, route by data quality,
choose between dev/prod sinks.
"""

from datetime import datetime

from airflow.sdk import dag, task
from airflow.utils.trigger_rule import TriggerRule


@dag(
    dag_id="example_04_branching",
    start_date=datetime(2026, 1, 1),
    schedule=None,
    catchup=False,
    tags=["example", "branching"],
)
def branching_example():
    @task
    def fetch_record_count() -> int:
        # Pretend this counts rows in a source table.
        return 42

    @task.branch
    def choose_path(count: int) -> str:
        # Returning a task_id tells Airflow which branch to execute.
        # The other branch is marked "skipped".
        if count > 100:
            return "process_large"
        return "process_small"

    @task
    def process_small() -> str:
        return "handled small batch"

    @task
    def process_large() -> str:
        return "handled large batch"

    @task(trigger_rule=TriggerRule.NONE_FAILED_MIN_ONE_SUCCESS)
    def report() -> None:
        # Without this trigger rule, `report` would skip too because one of
        # its upstream branches was skipped. NONE_FAILED_MIN_ONE_SUCCESS
        # says: run if at least one upstream succeeded and none failed.
        print("Reporting results")

    count = fetch_record_count()
    branch = choose_path(count)
    small = process_small()
    large = process_large()
    branch >> [small, large] >> report()


branching_example()
