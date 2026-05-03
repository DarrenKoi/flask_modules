"""
Example 03 — Mixing BashOperator with TaskFlow tasks.

Demonstrates:
- Importing operators from `apache-airflow-providers-standard`
  (in Airflow 3.x, BashOperator is no longer in airflow-core)
- Setting explicit dependencies with `>>`
- Using a Python task to read the bash task's stdout via XCom

When to use BashOperator vs @task: Bash for shell tools, file moves,
existing scripts. @task for anything you'd rather express in Python.
"""

from datetime import datetime

from airflow.providers.standard.operators.bash import BashOperator
from airflow.sdk import dag, task


@dag(
    dag_id="example_03_bash_operator",
    start_date=datetime(2026, 1, 1),
    schedule=None,
    catchup=False,
    tags=["example", "bash"],
)
def bash_example():
    # do_xcom_push=True (default) makes the last line of stdout available
    # to downstream tasks via XCom.
    list_files = BashOperator(
        task_id="list_files",
        bash_command="echo 'file1.txt file2.txt file3.txt'",
    )

    @task
    def count_files(file_list: str) -> int:
        files = file_list.split()
        print(f"Found {len(files)} files: {files}")
        return len(files)

    # Pull the bash task's XCom output and pass it to the python task.
    count_files(list_files.output)


bash_example()
