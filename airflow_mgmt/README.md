# airflow_mgmt

A learning + testing sandbox for Apache Airflow 3.1.8 DAGs.

The `dags/` folder is what you'll point Bitbucket at when registering the repo
with your company's Airflow platform. Everything else (tests, docs, compose
file) is for local development and never deployed.

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
│   └── windows_local_setup.md  # 3 ways to run Airflow on Windows
├── docker-compose.yaml         # full local Airflow 3.1.8 stack
├── requirements.txt            # versions matched to your platform
└── .env.example
```

## Three ways to test on Windows (pick one)

| Approach | When to use | Effort | Coverage |
|---|---|---|---|
| **DAG integrity tests** (`tests/`) | Syntax errors, import errors, structural checks | Low | Catches ~80% of pre-flight bugs |
| **`dag.test()` in pytest** | Verify task logic + XCom flow without a scheduler | Medium | Single-DAG end-to-end |
| **Docker Compose** | Anything UI-related, multi-DAG triggers, sensors, retries | High | Full Airflow stack locally |

**Start with the first one** — it requires only `pip install` and catches the
most common deployment failures (a DAG that fails to import in production
silently disappears from the UI).

See `docs/windows_local_setup.md` for full instructions.

## Quick start (DAG integrity tests only)

```bash
# from the repo root
cd airflow_mgmt
python -m venv .venv
.venv\Scripts\activate           # Windows PowerShell:  .venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m pytest tests/ -v
```

If all tests pass, your DAGs will at least *load* on your company's Airflow.

## Quick start (full local Airflow)

Requires Docker Desktop on Windows (uses WSL2 backend).

```bash
cd airflow_mgmt
copy .env.example .env
docker compose up -d
# UI: http://localhost:8080  (user: airflow / pass: airflow)
```

Stop with `docker compose down`. Add `-v` to wipe the metadata DB.

## Deployment to your company's Airflow

You don't deploy from here. The flow is:

1. Push this repo to Bitbucket.
2. Register the Bitbucket URL in your company's Airflow platform.
3. Airflow's **dag-processor** will pull the `dags/` folder and parse each file.
4. Anything that imports cleanly shows up in the UI.

This is why DAG integrity tests matter: a broken import = invisible DAG.
