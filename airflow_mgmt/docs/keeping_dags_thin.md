# Keeping DAGs thin: extract long Python into helper modules

When a DAG file grows past one screen, it stops being readable as a pipeline
overview and starts being a mix of "what runs when" and "how each step
works". Reviewers can't see the shape of the pipeline at a glance, and the
business logic can't be unit-tested without instantiating a DAG.

The fix: **DAG file = orchestration. Helper module = pure Python logic.**

See `dags/example_07_external_module.py` and `dags/util/orders.py` for a
working reference.

---

## The recommended pattern

Three files, three roles:

| File | Role | Imports `airflow`? |
|---|---|---|
| `dags/util/orders.py` | Pure business logic — parse, filter, transform | No |
| `dags/example_07_external_module.py` | DAG: schedule + `@task` wrappers + wiring | Yes |
| `tests/test_orders_lib.py` | Unit tests on the helpers | No |

The DAG file stays a one-page overview. The "long" logic lives next door
and is testable with plain `pytest`, no Airflow needed.

### Layout

```
airflow_mgmt/
└── dags/
    ├── util/                         ← helper subpackage (reusable across tasks)
    │   ├── __init__.py
    │   ├── orders.py                 ← pure functions, no airflow import
    │   └── transforms.py             ← add more as the project grows
    └── example_07_external_module.py ← thin DAG, imports from util.orders
```

### How the imports resolve

Airflow's dag-processor adds the `dags/` folder to `sys.path` automatically.
That makes any subpackage of `dags/` importable as a top-level name:

```python
# inside dags/example_07_external_module.py
from util.orders import parse_orders, daily_summary
```

`tests/conftest.py` does the same trick for pytest:

```python
DAGS_DIR = Path(__file__).resolve().parent.parent / "dags"
sys.path.insert(0, str(DAGS_DIR))
```

So the same `from util.orders import ...` works in production AND in tests.
No `PYTHONPATH` env var, no `sys.path.append` in the DAG file.

### What goes in `util/*.py`

- Pure functions and dataclasses
- No `airflow.*` imports
- No I/O against external systems (databases, APIs) — pass data in/out
- Anything you'd want to call from a Flask route or a CLI script too

### What stays in the DAG file

- `@dag(...)` decorator: schedule, start_date, retries, tags
- `@task` wrappers — usually 1–3 lines that call into `util/`
- Task wiring (the data flow `load(transform(extract()))`)
- Hooks/connections that *must* go through Airflow

---

## When NOT to use this pattern

| Situation | Use instead | Why |
|---|---|---|
| Helper has dep conflicts with Airflow (e.g. `pandas==1.5` while Airflow pins newer) | `@task.virtualenv` or `@task.external_python` | Runs the function in an isolated interpreter; args/return ship via pickle |
| Calling a vendor CLI or legacy script you don't own (`python migrate.py --date=...`) | `BashOperator(bash_command="python /path/to/script.py ...")` | Wasn't built to be imported; wrapping it as a function is more code than it's worth |
| Heavy CPU/memory work, or needs system packages | `KubernetesPodOperator` / `DockerOperator` | Full process isolation; doesn't share the worker's resources |
| The Python is 5–10 lines | Inline `@task` | Extracting would just add indirection |

### `@task.virtualenv` example

```python
from airflow.sdk import dag, task

@task.virtualenv(
    requirements=["pandas==1.5.3"],
    system_site_packages=False,
)
def transform_with_old_pandas(rows: list[dict]) -> dict:
    import pandas as pd
    df = pd.DataFrame(rows)
    return df.describe().to_dict()
```

The function body runs in a fresh venv each task run. Inputs and outputs
must be picklable. First run is slow (builds the venv); subsequent runs
reuse the cached venv.

### `BashOperator` example (vendor script)

```python
from airflow.providers.standard.operators.bash import BashOperator

run_vendor_etl = BashOperator(
    task_id="run_vendor_etl",
    bash_command="python /opt/vendor/etl.py --date {{ ds }}",
)
```

Trade-offs you accept: no Python return value via XCom (only the last
stdout line), tracebacks become subprocess stderr, and Airflow can't
introspect what failed beyond exit code.

---

## Anti-patterns

1. **Inlining 200 lines inside one `@task`.** Unreviewable, and you can't
   unit-test the logic without instantiating the DAG.

2. **`BashOperator(bash_command="python my_etl.py")` when `my_etl.py` is
   your own code.** You lose XCom return values, lose proper tracebacks,
   and Airflow can't introspect the failure. Import the function and call
   it from `@task` instead.

3. **Putting helpers in a sibling folder of `dags/`** (e.g.
   `airflow_mgmt/util/`). Your platform git-registers `dags/` only —
   anything outside is invisible in production. Subpackages of `dags/`
   are the only safe place.

4. **`sys.path.append(...)` at the top of a DAG file.** Brittle, hides
   the dependency, and surprises the next maintainer. Use a subpackage
   under `dags/` instead — it's importable for free.

5. **Relative imports inside a DAG file** (`from .util.orders import ...`).
   The dag-processor loads each DAG file as a top-level script, not as
   part of a package, so relative imports fail and the DAG silently
   disappears from the UI. Use absolute: `from util.orders import ...`.

6. **Reaching into Airflow internals from `util/`.** If `util/orders.py`
   imports `airflow`, you've coupled your business logic to Airflow's
   release cadence. Keep the helper layer Airflow-free; let the DAG
   layer adapt between Airflow's API and your pure functions.

---

## Verifying the split

After extracting, check both halves still work independently:

```bash
# 1. Helpers run on their own (no Airflow needed in this venv)
python -m pytest tests/test_orders_lib.py -v

# 2. DAG still parses (catches missing imports, typos)
python -m pytest tests/test_dag_integrity.py -v

# 3. Full quick check
python scripts/validate_dags.py
```

If the helper tests pass but DAG integrity fails, the split itself is
fine — you have a typo in the DAG file's imports or wiring. If helper
tests fail to even collect, your `from util.X import Y` path is wrong
(check `dags/util/__init__.py` exists and `tests/conftest.py` is putting
`dags/` on `sys.path`).
