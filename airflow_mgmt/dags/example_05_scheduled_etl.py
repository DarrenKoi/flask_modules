"""
Example 05 — A realistic scheduled ETL with retries and timeouts.

Demonstrates:
- A real schedule (daily at 02:00 UTC)
- default_args applied to every task (retries, retry_delay, timeout)
- Logical date / data interval awareness (the "logical_date" macro)
- Idempotent design: each run handles exactly one day of data

This is the shape most production DAGs converge on.
"""

from datetime import datetime, timedelta

from airflow.sdk import dag, task

DEFAULT_ARGS = {
    "owner": "data-team",
    "retries": 2,
    "retry_delay": timedelta(minutes=5),
    "execution_timeout": timedelta(minutes=30),
}


@dag(
    dag_id="example_05_scheduled_etl",
    description="Daily ingest of yesterday's order data",
    start_date=datetime(2026, 1, 1),
    schedule="0 2 * * *",        # 02:00 UTC every day (cron)
    catchup=False,               # don't backfill old runs on first deploy
    max_active_runs=1,           # one run at a time — prevents overlap
    default_args=DEFAULT_ARGS,
    tags=["example", "etl", "daily"],
)
def daily_orders_etl():
    @task
    def extract_for_day(data_interval_start=None) -> dict:
        # Airflow injects `data_interval_start` automatically. It's a pendulum
        # datetime representing the start of the period this run covers.
        # For a daily DAG, this is "yesterday at 00:00 UTC".
        day = data_interval_start.strftime("%Y-%m-%d") if data_interval_start else "unknown"
        print(f"Extracting orders for {day}")
        return {"day": day, "rows": 1234}

    @task
    def validate(payload: dict) -> dict:
        if payload["rows"] == 0:
            raise ValueError(f"No rows for {payload['day']} — upstream broken?")
        return payload

    @task
    def load_to_warehouse(payload: dict) -> None:
        print(f"Loaded {payload['rows']} rows for {payload['day']}")

    load_to_warehouse(validate(extract_for_day()))


daily_orders_etl()
