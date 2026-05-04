# airflow_mgmt

A learning + testing sandbox for Apache Airflow 3.1.8 DAGs.

The `dags/` folder is what you'll point Bitbucket at when registering the repo
with your company's Airflow platform. Everything else (tests, docs) is for
local development and never deployed.

## Layout

```
airflow_mgmt/
├── dags/                       # ← register THIS folder with your Airflow platform
│   ├── example_01_hello_world.py
│   ├── example_02_taskflow_etl.py
│   ├── example_03_bash_operator.py
│   ├── example_04_branching.py
│   ├── example_05_scheduled_etl.py
│   └── example_06_xcom_and_params.py
├── tests/                      # pytest / unittest — run anywhere, no Airflow server
│   ├── conftest.py
│   └── test_dag_integrity.py
├── scripts/
│   └── validate_dags.py        # quick parse-only check, no scheduler
├── docs/
│   └── windows_local_setup.md  # 2 ways to run Airflow on Windows
└── requirements.txt            # versions matched to your platform
```

## Constraint: no local Airflow server

Airflow is managed centrally by your company; you cannot run your own
Airflow stack (Docker Compose, Kubernetes, etc.). Local "testing" means one
of two things:

| Approach | When to use | Effort | Coverage |
|---|---|---|---|
| **DAG integrity tests** (`tests/`) | Syntax errors, import errors, structural checks | Low | Catches ~80% of pre-deploy bugs |
| **WSL2 + `airflow standalone`** | Verify scheduler/XCom/retry behavior end-to-end | Medium | Single-process Airflow on your laptop |

**Start with the first one** — it requires only `pip install` and catches the
most common deployment failures (a DAG that fails to import in production
silently disappears from the UI).

See `docs/windows_local_setup.md` for full instructions.

## Quick start (DAG integrity tests)

```bash
# from the repo root
cd airflow_mgmt
python -m venv .venv
.venv\Scripts\activate           # Windows PowerShell:  .venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m pytest tests/ -v
```

If all tests pass, your DAGs will at least *load* on your company's Airflow.

## Deployment to your company's Airflow

You don't deploy from here. The flow is:

1. Push this repo to Bitbucket.
2. Register the Bitbucket URL in your company's Airflow platform.
3. Airflow's **dag-processor** will pull the `dags/` folder and parse each file.
4. Anything that imports cleanly shows up in the UI.

This is why DAG integrity tests matter: a broken import = invisible DAG.
