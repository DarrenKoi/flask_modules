# airflow_mgmt

A learning + testing sandbox for Apache Airflow 3.1.8 DAGs.

The company Airflow platform references this git repo, but the worker may not
put the repo checkout on `sys.path` by default. DAGs and task helpers carry a
small bootstrap stub at the top of the file that locates the `airflow_mgmt/`
directory (by walking up to the first parent containing the
`project_root.txt` marker file) and inserts it into `sys.path` before
importing repo-local packages such as `minio_handler`. See
`docs/sys_path_bootstrap.md`.

## What's real vs. educational

This sandbox mixes production-bound code with learning material. Treat them
differently:

| Folder / file | Status | Notes |
|---|---|---|
| `dags/` | **REAL** | Registered with the company Airflow via Bitbucket. Files here will actually run (currently only `dags/diagnostics/inspect_packages_dag.py`). |
| `minio_handler/` | **REAL** | Repo-local package, importable from DAGs after the `sys.path` bootstrap. |
| `tests/` | **REAL** | DAG integrity tests — run anywhere, no Airflow server needed. |
| `dag_templates/` | **EDUCATIONAL** | Copy-paste boilerplate showing four DAG-authoring styles. Lives outside `dags/` so Airflow never auto-loads it. Nothing here runs on the company Airflow. |
| `scripts/ftp_download_sample.py` | **EDUCATIONAL** | Reference example of the FTP → MinIO pattern; not deployed. |
| `requirements/` | mixed | One file per `@task.virtualenv` use case (currently `probe_task.txt`, paired with `dag_templates/virtualenv_task_template.py` — also educational). |
| `utils/` | empty | Placeholder for future repo-local pure-Python helpers. |

Optional environment variables:

- `AIRFLOW_MGMT_ROOT`: absolute path to the checked-out `airflow_mgmt/` folder.
  Set this on the Airflow worker if the parent-walk can't find the
  `project_root.txt` marker (e.g. the repo is mounted at a path that isn't
  an ancestor of the DAG file, or the marker file was renamed/removed).
- `AIRFLOW_MGMT_SCRATCH_ROOT`: writable runtime scratch folder for downloads.
  Used by `scripts/ftp_download_sample.py`. Defaults to `/tmp/airflow_mgmt/`
  on Airflow workers and `airflow_mgmt/scratch/` for local development.

Never write task files into the git checkout. Use the scratch root and remove
per-run folders after the task finishes.

## Layout

```
airflow_mgmt/
├── project_root.txt               # sentinel ("do not remove") — marks this dir as the sys.path target
├── dags/                            # ← REAL — registered with the Airflow platform
│   └── diagnostics/
│       └── inspect_packages_dag.py  # list installed Python packages on the worker
├── dag_templates/                   # EDUCATIONAL — copy-paste DAG boilerplate (never auto-loaded)
│   ├── taskflow_decorator_template.py
│   ├── python_operator_template.py
│   ├── virtualenv_task_template.py
│   └── mixed_styles_template.py
├── requirements/                    # per-task pip requirements for @task.virtualenv
│   └── probe_task.txt
├── minio_handler/                   # repo-local MinIO/S3 wrapper (importable from DAGs)
├── utils/                           # empty placeholder for future repo-local helpers
├── scripts/
│   └── ftp_download_sample.py       # EDUCATIONAL — FTP→MinIO reference example
├── tests/                           # pytest — run anywhere, no Airflow server
│   ├── conftest.py
│   └── test_dag_integrity.py        # every DAG in dags/ must import cleanly
├── docs/
│   ├── windows_local_setup.md       # 2 ways to run Airflow on Windows
│   ├── keeping_dags_thin.md         # extract long Python into helper modules
│   └── sys_path_bootstrap.md        # why each entry-point file has the sys.path stub
└── requirements.txt                 # worker-side deps (Airflow + dev tooling)
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
