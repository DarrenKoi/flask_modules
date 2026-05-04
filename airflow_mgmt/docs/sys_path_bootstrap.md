# `sys.path` bootstrap: how DAGs find their own code

This doc explains the small bootstrap stub at the top of every DAG file
and entry-point script in this repo, and why it's there. There is **no**
shared `runtime_paths.py` module — the stub is the entire mechanism.

---

## The problem

Apache Airflow's dag-processor parses each `.py` file under the configured
DAGs folder as a **top-level script**, not as part of a package. That means:

1. Relative imports (`from .helpers import x`) fail — there is no parent
   package.
2. The dag-processor's `sys.path` contains the DAGs folder and Airflow's
   own paths, but **not necessarily** the rest of your repo. Workers may
   be started from `/ops/airflow`, the checkout may live at
   `/srv/repos/airflow_mgmt/`, and the only thing on `sys.path` is the
   `dags/` folder.
3. A DAG that fails to import does not appear with an error in the UI —
   it silently disappears. Bootstrap failures are nasty to debug in
   production.

The fix: **before importing anything repo-local, make sure `airflow_mgmt/`
is on `sys.path`.** Once it is, every `scripts/`, `minio_handler/`,
`utils/` package becomes importable as a top-level name, and the same
`from minio_handler import MinioObject` works in DAGs, in `pytest`, and in
standalone scripts.

---

## The bootstrap stub

This block goes at the top of every DAG file or entry-point script that
needs to import repo-local packages. It is intentionally duplicated across
files — see "Why no shared module?" below.

```python
import os, sys
from pathlib import Path

# ── sys.path bootstrap ──────────────────────────────────────────────────────
ROOT_DIR = Path(os.getenv("AIRFLOW_MGMT_ROOT") or next(
    (str(p) for p in Path(__file__).resolve().parents if p.name == "airflow_mgmt"),
    "",
)).resolve()
if not ROOT_DIR.is_dir():
    raise RuntimeError("Cannot find airflow_mgmt root. Set AIRFLOW_MGMT_ROOT.")
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))
# ────────────────────────────────────────────────────────────────────────────

# repo-local imports go here, after the bootstrap
from minio_handler import MinioObject     # noqa: E402
from utils.orders import daily_summary    # noqa: E402
```

Reading it line by line:

1. **`os.getenv("AIRFLOW_MGMT_ROOT")`** — env override wins. If the
   variable is set, use it verbatim (no auto-detect). Returns `None` if
   the variable isn't defined.
2. **`or next(...)`** — fall back to walking up from this file looking
   for a parent directory named `airflow_mgmt`. This is what makes the
   stub work on any worker where the file lives somewhere under an
   `airflow_mgmt/` folder.
3. **`""` default**: if the walk finds nothing, `next(...)` returns `""`,
   which becomes `Path("").resolve()` → `cwd`. The next check catches
   that.
4. **`if not ROOT_DIR.is_dir(): raise`** — fail loudly at *parse time*,
   not later inside a task. Easier to spot in the dag-processor logs
   than a `ModuleNotFoundError` raised mid-task.
5. **`sys.path.insert(0, str(ROOT_DIR))`** — put `airflow_mgmt/` first
   so our packages shadow any unrelated module of the same name on the
   worker.
6. **`# noqa: E402`** on subsequent imports — flake8 will flag them as
   "module-level import not at top of file". The flag is intentional;
   the bootstrap *must* run first.

---

## The full chain: DAG → script → helper

```
                          [bootstrap stub]
                                 │
                                 ▼
airflow_mgmt/  ──── added to sys.path ────►  Python can now import
    │                                         scripts/, minio_handler/, utils/
    │
    ├── dags/<topic>/foo_dag.py
    │       [bootstrap stub]
    │       from scripts.ftp_download_sample import collect_logs  ◄─ repo-local
    │       @task                                                    import
    │       def download_and_upload(ips): return collect_logs(ips)
    │
    ├── scripts/
    │   └── ftp_download_sample.py
    │       [bootstrap stub]
    │       from minio_handler import MinioObject     ◄─── another repo-local
    │       def collect_logs(ips: list[str]) -> dict: ...    import
    │
    └── minio_handler/
        ├── __init__.py
        └── object.py    (defines MinioObject)
```

Three-level chain, all enabled by the same `sys.path.insert(0,
airflow_mgmt/)`:

1. The DAG file bootstraps → imports `scripts.ftp_download_sample.collect_logs`.
2. `scripts/ftp_download_sample.py` bootstraps too. The second
   `sys.path.insert` is idempotent (the path is already there), so this
   is a cheap no-op when it's reached via `import`. It matters when the
   script is run standalone (`python scripts/ftp_download_sample.py`).
3. `minio_handler/object.py` doesn't bootstrap — by the time anyone
   imports it, `sys.path` is already set up.

The rule of thumb: **only files that are entry points need the stub**.
A DAG file is an entry point (the dag-processor parses it as a top-level
script). A standalone CLI like `scripts/ftp_download_sample.py` is an
entry point. Pure helper modules that are only ever imported are not
entry points — they inherit the caller's `sys.path`.

---

## When *not* to bootstrap

Not every DAG needs the stub. The right rule:

> If the DAG file imports nothing from `scripts/`, `minio_handler/`,
> `utils/`, or any other repo-local package, **don't include the
> bootstrap stub**.

`dags/diagnostics/inspect_packages_dag.py` is the model. It only uses
stdlib (`importlib.metadata.distributions`) and `airflow.sdk`, so it
parses fine with whatever `sys.path` Airflow gives it. Adding a stub
there would add a parse-time failure mode for zero benefit.

---

## The two paths: `ROOT_DIR` (code) vs `SCRATCH_ROOT` (data)

Conflating "where the code lives" with "where I write files" is the
classic "PermissionError: cannot mkdir under the dags folder" bug.

| Variable | Set by | Where it points (Airflow) | Where it points (local dev) | Use for |
|---|---|---|---|---|
| `ROOT_DIR` | the bootstrap stub | `/opt/airflow/dags/repo/airflow_mgmt/` (read-only git mount) | `airflow_mgmt/` (your checkout) | locating bundled config files, **never writes** |
| `SCRATCH_ROOT` | `_scratch_root()` in `scripts/ftp_download_sample.py` | `/tmp/airflow_mgmt/` | `airflow_mgmt/scratch/` | downloads, working files, anything you `mkdir` or write |

Why two? On a managed Airflow cluster, the worker has **read** access to
the git mount but ops typically deny **write** access by design — they
don't want tasks polluting the source tree. So scratch goes elsewhere.

Currently only `scripts/ftp_download_sample.py` needs scratch handling,
so the helper lives inline in that file. If a second script needs it,
you can either copy the helper or factor it into a shared module —
whichever is right at the time.

---

## Environment variables

```
AIRFLOW_MGMT_ROOT          → absolute path to airflow_mgmt/   (used by bootstrap stub)
AIRFLOW_MGMT_SCRATCH_ROOT  → absolute path to a writable runtime dir   (used by ftp_download_sample.py)
```

Both are **OS environment variables** — not Airflow Variables, not config
file entries. They're read with `os.getenv(...)`. To set them:

**PowerShell (current session):**
```powershell
$env:AIRFLOW_MGMT_ROOT = "C:\Code\flask_modules\airflow_mgmt"
$env:AIRFLOW_MGMT_SCRATCH_ROOT = "D:\airflow_scratch"
```

**PowerShell (persistent for your user):**
```powershell
[Environment]::SetEnvironmentVariable("AIRFLOW_MGMT_ROOT", "C:\Code\flask_modules\airflow_mgmt", "User")
```

**bash / zsh:**
```bash
export AIRFLOW_MGMT_ROOT=/srv/repos/airflow_mgmt
```

**On the Airflow cluster:** ops sets these in the systemd unit, the
Kubernetes Pod spec, the Helm values, the docker-compose `environment:`
block — wherever the scheduler / dag-processor / worker processes are
launched.

Set `AIRFLOW_MGMT_ROOT` only when auto-detect fails (the parent walk
can't find a directory named `airflow_mgmt`). Set
`AIRFLOW_MGMT_SCRATCH_ROOT` when `/tmp` is the wrong filesystem
(too small, slow, or wrong volume).

---

## Anti-patterns

1. **`sys.path.append("/some/hard/coded/path")`** — works on one Airflow
   cluster, breaks on the next. Use the stub.
2. **`from .helpers import x`** in a DAG file — relative imports fail
   because dag-processor parses each DAG as a top-level script, not as
   part of a package.
3. **Writing under `ROOT_DIR`** — that's the read-only git mount on
   Airflow workers. Use `SCRATCH_ROOT` for any `mkdir`/`open(..., "w")`.
4. **Adding the stub to a DAG that doesn't need it** — extra parse-time
   failure mode for zero benefit. Skip the stub when there are no
   repo-local imports.
5. **Forgetting `# noqa: E402` on the post-bootstrap imports** — flake8
   will flag them. The `noqa` is intentional; the bootstrap must run
   before any repo-local import.
6. **Reaching into Airflow internals from `utils/`** — keeps the helper
   layer Airflow-free so the DAG layer alone adapts to Airflow API
   changes.

---

## Why no shared module?

You might wonder why the stub is duplicated across 5 files instead of
factoring it into a helper module. The reason is the chicken-and-egg:

> To `from helpers.bootstrap import setup_path` you need
> `helpers.bootstrap` already importable. But getting helpers importable
> is what `setup_path` was supposed to do.

So the very first `sys.path.insert` *has to* live in each entry-point
file. We could put extra logic *after* that line in a shared module, but
the only logic worth sharing right now is `~3 lines`. A module with that
much code in it would be more indirection than help.

Templates also need to be **copy-pasteable**: dropping
`templates/taskflow_decorator_template.py` into `dags/<topic>/foo_dag.py`
should produce a working DAG with no edits to other files. A shared
helper module would force every template-copy operation to also pull in
that module's path — defeating the point.

If a third or fourth distinct piece of bootstrap logic shows up, that's
the point to revisit. Right now, 9 lines × 5 files is the most readable
trade-off.

---

## Verifying it works

```bash
# DAG integrity — every DAG file parses cleanly
python -m pytest airflow_mgmt/tests/test_dag_integrity.py -v

# Helper unit tests — pure Python, no Airflow needed
python -m pytest airflow_mgmt/tests/test_orders_lib.py -v

# Quick parse-only check before pushing
python airflow_mgmt/scripts/validate_dags.py
```

If a DAG silently disappears in the production UI, that means it failed
to import. Run `validate_dags.py` locally — it will reproduce the parse
error and tell you which file.
