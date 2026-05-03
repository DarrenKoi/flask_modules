"""
Example 06 — DAG params (runtime user input) + explicit XCom.

Demonstrates:
- params: typed runtime inputs the user fills in via the UI "Trigger DAG w/ config"
- Reading params inside a task via the `params` argument injection
- Returning multiple values and accessing them downstream

When to use params: anything a human chooses at trigger time
(target environment, date range, dry-run flag, etc.).
"""

from datetime import datetime

from airflow.sdk import Param, dag, task


@dag(
    dag_id="example_06_xcom_and_params",
    start_date=datetime(2026, 1, 1),
    schedule=None,
    catchup=False,
    tags=["example", "params", "xcom"],
    params={
        # Each Param accepts a default + JSON-schema-style validators.
        # The Airflow UI renders these as form fields.
        "target_env": Param("staging", type="string", enum=["staging", "prod"]),
        "limit": Param(10, type="integer", minimum=1, maximum=1000),
        "dry_run": Param(True, type="boolean"),
    },
)
def params_and_xcom():
    @task
    def show_params(params=None) -> dict:
        # Airflow injects `params` (a dict-like) when the kwarg is present.
        env = params["target_env"]
        limit = params["limit"]
        dry_run = params["dry_run"]
        print(f"Running against {env} with limit={limit} dry_run={dry_run}")
        return {"env": env, "limit": limit, "dry_run": dry_run}

    @task(multiple_outputs=True)
    def split_metrics() -> dict[str, int]:
        # multiple_outputs=True turns each dict key into its own XCom entry,
        # so downstream tasks can request individual values.
        return {"success": 95, "failed": 5}

    @task
    def summarize(config: dict, success: int, failed: int) -> None:
        total = success + failed
        rate = success / total if total else 0
        print(f"[{config['env']}] success={success}/{total} rate={rate:.1%}")

    cfg = show_params()
    metrics = split_metrics()
    summarize(cfg, metrics["success"], metrics["failed"])


params_and_xcom()
