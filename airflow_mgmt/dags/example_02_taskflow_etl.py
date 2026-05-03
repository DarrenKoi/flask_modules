"""
Example 02 — TaskFlow ETL pattern.

Demonstrates:
- Multiple @task functions chained as Python calls
- Return values flowing through XCom automatically
- The classic Extract → Transform → Load shape

The big win of TaskFlow: tasks are just Python functions. The arrows between
them are inferred from how you pass return values. No manual `>>` wiring.
"""

from datetime import datetime

from airflow.sdk import dag, task


@dag(
    dag_id="example_02_taskflow_etl",
    start_date=datetime(2026, 1, 1),
    schedule=None,
    catchup=False,
    tags=["example", "taskflow"],
)
def taskflow_etl():
    @task
    def extract() -> dict[str, int]:
        # Pretend this is a DB / API call.
        return {"orders": 120, "refunds": 5, "errors": 2}

    @task
    def transform(raw: dict[str, int]) -> dict[str, float]:
        net = raw["orders"] - raw["refunds"]
        error_rate = raw["errors"] / max(raw["orders"], 1)
        return {"net_orders": net, "error_rate": round(error_rate, 4)}

    @task
    def load(metrics: dict[str, float]) -> None:
        # Pretend this writes to a warehouse.
        print(f"Loading metrics: {metrics}")

    # Dependencies are inferred from the data flow:
    # extract → transform → load
    load(transform(extract()))


taskflow_etl()
