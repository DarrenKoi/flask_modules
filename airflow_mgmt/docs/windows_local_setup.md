# Running Airflow 3.1.8 locally on Windows

Airflow does **not run natively on Windows** — the scheduler relies on POSIX
fork semantics. And since your company manages the Airflow platform centrally,
running your own containerised Airflow stack is **not** an option here. That
leaves two workable paths:

1. **DAG integrity tests** — pip install + pytest. No scheduler.
2. **WSL2 + `airflow standalone`** — single-process Airflow inside WSL.

Pick the lightest option that answers your current question.

---

## Option 1 — DAG integrity tests (fastest feedback)

Catches imports, syntax, cycles, and structural bugs. ~80% of pre-deploy
failures show up here. Works on plain Windows Python with no WSL.

```powershell
cd airflow_mgmt
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m pytest tests/ -v
```

Or for a one-shot summary without pytest:

```powershell
python scripts\validate_dags.py
```

> **Note**: `pip install apache-airflow` on plain Windows Python sometimes
> chokes on transitive dependencies (e.g. `python-daemon`). If that
> happens, use Option 2 (WSL) — Airflow supports Linux first-class. The
> integrity tests themselves don't actually need Airflow installed when
> the DAG files only import from `airflow.sdk` / providers that ship pure
> Python.

---

## Option 2 — WSL2 + `airflow standalone` (recommended for daily dev)

`airflow standalone` runs the scheduler, dag-processor, and API server in a
single process, with a SQLite metadata DB. It boots in ~10 seconds and is
plenty for testing your own DAGs.

### One-time WSL setup

In PowerShell as Administrator:

```powershell
wsl --install -d Ubuntu
```

Reboot, finish Ubuntu's first-run prompt, then inside the Ubuntu shell:

```bash
sudo apt update && sudo apt install -y python3.11 python3.11-venv python3-pip
```

### Each session

```bash
cd /mnt/c/Users/<you>/Codes/flask_modules/airflow_mgmt
python3.11 -m venv .venv
source .venv/bin/activate

CONSTRAINT="https://raw.githubusercontent.com/apache/airflow/constraints-3.1.8/constraints-3.11.txt"
pip install "apache-airflow==3.1.8" "apache-airflow-providers-standard" --constraint "$CONSTRAINT"

export AIRFLOW_HOME=$(pwd)/.airflow
export AIRFLOW__CORE__DAGS_FOLDER=$(pwd)/dags
export AIRFLOW__CORE__LOAD_EXAMPLES=False

airflow standalone
```

The console prints the auto-generated admin password. UI lands at
http://localhost:8080.

To trigger a DAG from the CLI:

```bash
airflow dags trigger ftp_ingest_worker
airflow dags list-runs --dag-id ftp_ingest_worker
```

To run a DAG **without the scheduler at all** (one-shot, in-process):

```bash
airflow dags test ftp_ingest_worker 2026-01-01
```

That last command is the closest you'll get to "just run my DAG and show me
the output" — no UI required.

To exercise a single task in the foreground (fastest debug loop):

```bash
airflow tasks test ftp_ingest_worker download_and_upload 2026-01-01
```

---

## Which one when?

| Situation | Use |
|---|---|
| "Will this DAG even load on the company platform?" | Option 1 |
| "Does my Python logic produce the right output?" | Option 1 (unit-test the function) or `airflow dags test` (Option 2) |
| "Does my schedule + retry config work as I expect?" | Option 2 |
| "I'm using a sensor / trigger / multi-DAG dependency" | Option 2 |

Default workflow: **Option 1 in CI and on every save, Option 2 on your laptop**
when you need the scheduler in the loop. Anything you can't reproduce in
Option 2 has to be tested by deploying to a non-prod folder on the actual
company Airflow platform.
