"""
diagnostics / inspect_packages_dag.

Trigger this DAG manually to list Python packages installed on the
Airflow worker. Useful for "is package X already available?" questions
without needing shell access to the cluster.

The task prints results to its log so they show up in the Airflow UI
(Graph → task → Logs). It also returns the list as XCom so downstream
tasks (or `airflow tasks test`) can consume it programmatically.

Trigger options (set via "Trigger DAG w/ config" in the UI):
- search:        substring filter applied to package names (case-insensitive)
- show_location: if true, include each distribution's install path

Examples:
  {}                                  → list all packages
  {"search": "pandas"}                → only packages with "pandas" in the name
  {"search": "airflow", "show_location": true}
"""

from datetime import datetime
from importlib.metadata import distributions

from airflow.models.param import Param
from airflow.sdk import dag, task


@dag(
    dag_id="diagnostics_inspect_packages",
    description="List Python packages installed on the Airflow worker",
    start_date=datetime(2026, 1, 1),
    schedule=None,             # manual trigger only
    catchup=False,
    params={
        "search": Param(
            "",
            type="string",
            description="Substring filter on package names (case-insensitive). Empty = list all.",
        ),
        "show_location": Param(
            False,
            type="boolean",
            description="Include each distribution's install path in the output.",
        ),
    },
    tags=["diagnostics"],
)
def inspect_packages():
    @task
    def list_packages(params: dict) -> list[dict]:
        query = (params.get("search") or "").strip().lower()
        show_location = bool(params.get("show_location"))

        results: list[dict] = []
        for dist in distributions():
            name = (dist.metadata["Name"] or "<unknown>").strip()
            if query and query not in name.lower():
                continue
            entry = {"name": name, "version": dist.version}
            if show_location:
                # `_path` is private but stable; the public alternative is
                # `dist.locate_file('')` which gives the same root.
                entry["location"] = str(dist.locate_file(""))
            results.append(entry)

        results.sort(key=lambda r: r["name"].lower())

        header = f"{len(results)} package(s) installed"
        if query:
            header += f" matching {query!r}"
        print(f"# {header}:")
        for r in results:
            line = f"  {r['name']}=={r['version']}"
            if show_location:
                line += f"   ({r['location']})"
            print(line)

        return results

    list_packages()


inspect_packages()
