# Running Airflow 3.1.8 locally on Windows

Airflow does **not run natively on Windows** — the scheduler relies on POSIX
fork semantics. You have three workable options, in increasing order of
"closeness to production":

1. **DAG integrity tests** — pip install + pytest. No scheduler.
2. **WSL2 + `airflow standalone`** — single-process Airflow inside WSL.
3. **Docker Compose** — full Airflow stack (the same image your company runs).

Pick the lightest option that answers your current question.

---

## Option 1 — DAG integrity tests (fastest feedback)

Catches imports, syntax, cycles, and structural bugs. ~80% of pre-deploy
failures show up here. Works on plain Windows Python with no WSL or Docker.

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
> happens, use Option 2 (WSL) — Airflow supports Linux first-class.

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
airflow dags trigger example_01_hello_world
airflow dags list-runs --dag-id example_01_hello_world
```

To run a DAG **without the scheduler at all** (one-shot, in-process):

```bash
airflow dags test example_02_taskflow_etl 2026-01-01
```

That last command is the closest you'll get to "just run my DAG and show me
the output" — no UI required.

---

## Option 3 — Docker Compose (closest to production)

Use this when you need to test things the standalone mode can't cover:
multiple workers, sensors, retries-with-backoff timing, the actual API server.

### Prerequisites

- Docker Desktop with the WSL2 backend enabled
- 4 GB RAM allocated to Docker (Settings → Resources)

### Bring it up

```powershell
cd airflow_mgmt
copy .env.example .env
docker compose up airflow-init     # one-shot DB migrate + create admin
docker compose up -d               # start scheduler/api-server/dag-processor
```

UI: http://localhost:8080  (user: `airflow`, pass: `airflow`)

The compose file mounts `./dags` into the container, so editing a DAG file
on Windows updates the running Airflow within seconds (the dag-processor
re-parses it).

### Tear down

```powershell
docker compose down       # stop containers, keep DB
docker compose down -v    # also wipe the postgres volume (start fresh)
```

### Useful commands inside the container

```powershell
docker compose exec airflow-scheduler airflow dags list
docker compose exec airflow-scheduler airflow dags test example_01_hello_world 2026-01-01
docker compose exec airflow-scheduler airflow tasks test example_02_taskflow_etl extract 2026-01-01
```

`airflow tasks test` runs a single task in the foreground — fastest way to
debug one task without triggering the whole DAG.

---

## Which one when?

| Situation | Use |
|---|---|
| "Will this DAG even load on the company platform?" | Option 1 |
| "Does my Python logic produce the right output?" | Option 1 (unit test the function) or `airflow dags test` (Option 2/3) |
| "Does my schedule + retry config work as I expect?" | Option 2 |
| "I'm using a sensor / trigger / multi-DAG dependency" | Option 3 |
| "I need to demo this to a teammate" | Option 3 (full UI) |

The default workflow once you're moving fast: **Option 1 in CI, Option 2 on
your laptop**, drop down to Option 3 only when something Option 2 can't
reproduce shows up.
