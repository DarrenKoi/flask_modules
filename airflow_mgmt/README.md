# airflow_mgmt

A learning + testing sandbox for Apache Airflow 3.1.8 DAGs.

The company Airflow platform references this git repo, but the worker may not
put the repo checkout on `sys.path` by default. DAGs and task helpers carry a
small bootstrap stub at the top of the file that locates the `airflow_mgmt/`
directory and inserts it into `sys.path` before importing repo-local packages
such as `utils` or `minio_handler`. See `docs/sys_path_bootstrap.md`.

Optional environment variables:

- `AIRFLOW_MGMT_ROOT`: absolute path to the checked-out `airflow_mgmt/` folder.
  Set this on the Airflow worker if the parent-walk can't find the directory
  (e.g. the repo is mounted at a path that isn't an ancestor of the DAG file).
- `AIRFLOW_MGMT_SCRATCH_ROOT`: writable runtime scratch folder for downloads.
  Used by `scripts/ftp_download_sample.py`. Defaults to `/tmp/airflow_mgmt/`
  on Airflow workers and `airflow_mgmt/scratch/` for local development.

Never write task files into the git checkout. Use the scratch root and remove
per-run folders after the task finishes.

## Layout

```
airflow_mgmt/
├── dags/                       # ← register THIS folder with your Airflow platform
│   ├── utils/                  # cross-topic helpers (pure Python, reusable across tasks)
│   │   ├── orders.py
│   │   └── minio_handler/      # vendored from project root for use in DAGs
│   ├── ftp_ingest/             # topic: download files from FTP servers
│   │   ├── sources.py          # config registry (200 entries in prod)
│   │   ├── lib/
│   │   │   └── downloader.py   # pure-Python ftplib wrapper
│   │   ├── ingest_dag.py       # @task: FTP → MinIO upload (runs on worker)
│   │   └── ingest_kpo_dag.py   # KubernetesPodOperator version
│   └── diagnostics/            # topic: inspect the Airflow runtime
│       └── inspect_packages_dag.py  # list installed Python packages
├── tests/                      # pytest / unittest — run anywhere, no Airflow server
│   ├── conftest.py
│   ├── test_dag_integrity.py
│   ├── test_orders_lib.py
│   └── test_ftp_downloader.py
├── scripts/
│   └── validate_dags.py        # quick parse-only check, no scheduler
├── docs/
│   ├── windows_local_setup.md  # 2 ways to run Airflow on Windows
│   └── keeping_dags_thin.md    # extract long Python into helper modules
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
3. Airflow's **dag-processor** will pull or mount the repo and parse DAG files.
4. Anything that imports cleanly after the runtime path bootstrap shows up in the UI.

This is why DAG integrity tests matter: a broken import = invisible DAG.
