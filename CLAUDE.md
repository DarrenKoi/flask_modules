# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

A personal toolbox of work-supporting Python modules. Each top-level package solves one piece of the day-to-day workflow (search, object storage, Airflow scaffolding, index setup). Packages are independent — pick one up and use it from a notebook, a script, or an Airflow task without dragging the rest along.

The directory name `flask_modules` is historical. There is no Flask app here anymore.

## Modules

- **`ops_store/`** — class-based `opensearch-py` wrapper. `OSConfig` (env-driven dataclass, `slots=True`) + `OSBase` + three services on top: `OSDoc` (single + bulk doc CRUD via the `_bulk_helper` indirection), `OSIndex` (index/alias management, `describe()` for rollover-style inspection), `OSSearch` (lexical, kNN, hybrid, aggregations — all delegate to `search_raw`). Public API lives in `ops_store/__init__.py` `__all__`. Tests patch `ops_store.base._opensearch_class` to avoid a real cluster. `ops_store` does not log its own calls — observe via OpenSearch/Kibana.
- **`minio_handler/`** — class-based MinIO / S3-compatible client. `MinioConfig` + `MinioBase` + `MinioObject` (CRUD, presigned URLs, DataFrame parquet round-trip via `put_dataframe` / `get_dataframe`, image helpers). Public API in `minio_handler/__init__.py`. Vendored copy lives under `airflow_mgmt/minio_handler/` so DAGs can import it without depending on the repo root being installed.
- **`ops_index_mgmt/`** — operational scripts that materialize specific OpenSearch indices (mappings, ISM policies, etc.) for the company cluster. One file per index/use case (e.g. `hitachi_sem_msr_info.py`).
- **`airflow_mgmt/`** — sandbox + production-bound code for the company Airflow 3.1.8 platform. Real DAGs in `dags/`, repo-local helpers in `utils/`, vendored `minio_handler/`, copy-paste scaffolding in `dag_templates/`, reference scripts in `scripts/`. See `airflow_mgmt/README.md` for the real-vs-educational split and `airflow_mgmt/docs/sys_path_bootstrap.md` for the marker-file `sys.path` trick every DAG uses.

## Conventions

- Python 3.11: `Self`, PEP 604 unions (`str | None`), `slots=True` dataclasses.
- Do **not** use `from __future__ import annotations`.
- 4-space indent, explicit imports, small direct functions.
- Filesystem paths: always `pathlib.Path`. No `os.path`, `os.sep`, or string concatenation. Compose with `/` or `joinpath`; return `Path` from helpers.
- No preemptive guardrails — don't add warnings/validation for parameters that are only wrong in the current environment. Trust the caller.
- Field names that vary per index (e.g. `time_field`) stay as required per-call args; don't mirror them onto instance defaults the way `default_index` is mirrored.
- Module / package names: `lowercase_with_underscores`. Test files: `test_*.py`. Service class names stay short: `OSDoc`, `OSIndex`, `OSSearch`, `MinioObject`.
- Tests use stdlib `unittest` + `unittest.mock`. Never require a live cluster or live MinIO — mock the client. Name tests by behavior (`test_bulk_index_builds_actions`); assert both return values and key client call kwargs.
- Commits: short, imperative, lowercase (`add put_dataframe to minio_handler`).

## Common commands

```bash
python3.11 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Full test suite
python3 -m unittest discover -s tests -v

# Single test case or method
python3 -m unittest tests.test_ops_store_services.OSDocTests -v
python3 -m unittest tests.test_ops_store_services.OSDocTests.test_bulk_index_builds_actions -v

# Quick import/syntax validation (no linter configured)
python3 -m compileall ops_store minio_handler ops_index_mgmt tests

# Airflow DAG integrity tests (no Airflow server needed)
python3 -m pytest airflow_mgmt/tests -v
```

## Configuration surface

Env vars read by the modules — keep this list in sync if you add new ones:

- **OpenSearch connection** (`ops_store`): `OPENSEARCH_HOST`, `OPENSEARCH_PORT`, `OPENSEARCH_USER`, `OPENSEARCH_PASSWORD`, `OPENSEARCH_USE_SSL`, `OPENSEARCH_VERIFY_CERTS`, `OPENSEARCH_SSL_SHOW_WARN`, `OPENSEARCH_CA_CERTS`
- **OpenSearch tuning** (`ops_store`): `OPENSEARCH_BULK_CHUNK`, `OPENSEARCH_TIMEOUT`, `OPENSEARCH_MAX_RETRIES`, `OPENSEARCH_RETRY_ON_TIMEOUT`, `OPENSEARCH_HTTP_COMPRESS`
- **MinIO** (`minio_handler`): see `minio_handler/minio_config.py` (endpoint, access key, secret, secure flag, default bucket)
- **Airflow scratch** (`airflow_mgmt`): `AIRFLOW_MGMT_ROOT` (override marker-walk root), `AIRFLOW_MGMT_SCRATCH_ROOT` (writable scratch dir; defaults to `/tmp/airflow_mgmt/` on workers, `airflow_mgmt/scratch/` locally)

Note: there is no WSGI/Flask entrypoint anymore. `requirements.txt` no longer pins Flask.
