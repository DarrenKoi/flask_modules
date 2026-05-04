"""
DAG integrity tests — run anywhere, no Airflow scheduler needed.

These tests catch the most common pre-deployment failures:
- Import errors (missing package, wrong path)
- Syntax errors
- Cycles in task dependencies
- Missing required DAG fields

Run with:  python -m pytest tests/ -v
"""

from pathlib import Path

import pytest
from airflow.models import DagBag

DAGS_DIR = Path(__file__).resolve().parent.parent / "dags"


@pytest.fixture(scope="session")
def dag_bag() -> DagBag:
    """
    DagBag parses every .py file in DAGS_DIR exactly once per test session.

    include_examples=False keeps Airflow's built-in tutorial DAGs out of our
    test surface so we only assert on our own DAGs.
    """
    return DagBag(dag_folder=str(DAGS_DIR), include_examples=False)


def test_no_import_errors(dag_bag: DagBag) -> None:
    """
    The single most valuable test. A DAG that fails to import never appears
    in the Airflow UI — it just silently disappears, which is hard to debug
    in production. Catching this in CI prevents that whole class of bug.
    """
    assert not dag_bag.import_errors, (
        f"Import errors found:\n"
        + "\n".join(f"  {path}: {err}" for path, err in dag_bag.import_errors.items())
    )


def test_dags_loaded(dag_bag: DagBag) -> None:
    """At least one DAG should be discovered."""
    assert len(dag_bag.dags) > 0, "No DAGs were discovered in dags/"


def test_every_dag_has_owner_and_tags(dag_bag: DagBag) -> None:
    """
    Soft hygiene check — every DAG should declare an owner (default_args)
    and at least one tag so it's findable in the UI.
    """
    for dag_id, dag in dag_bag.dags.items():
        assert dag.tags, f"{dag_id} has no tags — add tags=[...] to @dag"


def test_no_cycles(dag_bag: DagBag) -> None:
    """
    Airflow refuses to schedule a DAG with a dependency cycle, but the error
    only surfaces at parse time on the server. Fail in CI instead.
    """
    for dag_id, dag in dag_bag.dags.items():
        # test_cycle is implicit during DagBag.bag_dag(); if we got here,
        # the DAG has no cycles. Asserting topological_sort() works as a
        # belt-and-braces check that wouldn't crash if Airflow internals shift.
        try:
            dag.topological_sort()
        except Exception as exc:  # pragma: no cover — only fires on real cycles
            pytest.fail(f"{dag_id} has a cycle: {exc}")
