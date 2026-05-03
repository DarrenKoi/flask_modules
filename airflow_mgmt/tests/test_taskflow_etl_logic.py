"""
Example of unit-testing task LOGIC (not orchestration).

Pattern: extract the pure Python work into a module-level function, then
test it directly. The @task decorator wraps a function but the underlying
callable is still accessible via .function in Airflow 3.x.

This is faster and clearer than dag.test() when you just want to verify
"does my transform produce the right number?"
"""

from pathlib import Path
import sys

# Ensure dags/ is on the path (also done in conftest.py, redundant guard)
DAGS_DIR = Path(__file__).resolve().parent.parent / "dags"
if str(DAGS_DIR) not in sys.path:
    sys.path.insert(0, str(DAGS_DIR))


def test_taskflow_etl_dag_runs_end_to_end() -> None:
    """
    DAG.test() executes a DAG run in-process — no scheduler, no DB connection
    to a real cluster, no UI. It's the simplest way to prove a DAG actually
    works on your machine.

    This is the canonical Airflow 3.x local-test pattern.
    """
    import example_02_taskflow_etl  # noqa: F401 — registers the DAG

    from airflow.models import DagBag

    bag = DagBag(dag_folder=str(DAGS_DIR), include_examples=False)
    dag = bag.get_dag("example_02_taskflow_etl")
    assert dag is not None

    # dag.test() runs every task in topological order in the current process.
    # It returns a DagRun object with task instance states you can inspect.
    dagrun = dag.test()
    assert dagrun is not None
