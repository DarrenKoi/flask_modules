"""
Example 07 — Keep DAGs thin by importing helpers from a sibling module.

Demonstrates:
- DAG file as orchestration only: schedule, dependencies, retries
- Business logic lives in `lib/orders.py` and is imported, not inlined
- Each @task is a 1–3 line wrapper that calls a tested pure function

Why this pattern:
- The DAG file fits on one screen — reviewers see the *shape* of the pipeline
- `lib/orders.py` is unit-testable with plain pytest, no Airflow needed
- Same helpers can be reused from other DAGs or a Flask endpoint

How imports resolve:
- Airflow's dag-processor puts `dags_folder` on sys.path
- `dags/lib/__init__.py` makes `lib` an importable package
- `from lib.orders import ...` works in production and in pytest (see conftest.py)
"""

from datetime import datetime, timedelta

from airflow.sdk import dag, task

from lib.orders import customer_totals, daily_summary, parse_orders


DEFAULT_ARGS = {
    "owner": "data-team",
    "retries": 2,
    "retry_delay": timedelta(minutes=5),
}


@dag(
    dag_id="example_07_external_module",
    description="Slim DAG — logic lives in dags/lib/orders.py",
    start_date=datetime(2026, 1, 1),
    schedule=None,
    catchup=False,
    default_args=DEFAULT_ARGS,
    tags=["example", "best-practice"],
)
def orders_pipeline():
    @task
    def extract() -> list[dict]:
        # In production: a hook or SQL query. Kept inline here so the example
        # is self-contained.
        return [
            {"order_id": "A1", "customer_id": "c1", "amount": "120.00",
             "status": "completed", "placed_on": "2026-05-03"},
            {"order_id": "A2", "customer_id": "c1", "amount": "45.50",
             "status": "completed", "placed_on": "2026-05-03"},
            {"order_id": "A3", "customer_id": "c2", "amount": "999.00",
             "status": "refunded", "placed_on": "2026-05-03"},
            {"order_id": "BAD", "amount": "oops"},  # malformed → dropped
        ]

    @task
    def transform(rows: list[dict]) -> dict:
        orders = parse_orders(rows)
        return {
            "summary": daily_summary(orders),
            "per_customer": customer_totals(orders),
        }

    @task
    def load(payload: dict) -> None:
        print(f"Summary: {payload['summary']}")
        print(f"Per customer: {payload['per_customer']}")

    load(transform(extract()))


orders_pipeline()
