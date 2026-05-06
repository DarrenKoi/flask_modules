# Repository Guidelines

## Project structure

Personal toolbox of work-supporting Python modules. Each top-level package is independent.

- `ops_store/` — class-based OpenSearch helpers (`base.py`, `document.py`, `index.py`, `search.py`)
- `minio_handler/` — class-based MinIO / S3-compatible client (`base.py`, `object.py`, `minio_config.py`)
- `ops_index_mgmt/` — operational scripts that materialize specific OpenSearch indices
- `airflow_mgmt/` — Airflow 3.1.8 DAGs, repo-local helpers, vendored `minio_handler`, templates, scripts (see `airflow_mgmt/README.md`)
- `tests/` — unit tests for the top-level modules

The repository name `flask_modules` is historical; there is no Flask app.

## Build / test commands

- `python3.11 -m venv .venv && source .venv/bin/activate` — create and activate a local environment
- `pip install -r requirements.txt` — install runtime deps (`opensearch-py`, `minio`, `redis`)
- `python3 -m unittest discover -s tests -v` — full unit-test suite
- `python3 -m compileall ops_store minio_handler ops_index_mgmt tests` — quick syntax/import validation
- `python3 -m pytest airflow_mgmt/tests -v` — Airflow DAG integrity tests (no server needed)

## Coding style

- Python 3.11. Use `Self`, PEP 604 unions (`str | None`), `slots=True` dataclasses.
- Do **not** use `from __future__ import annotations`.
- 4-space indent, explicit imports, small direct functions.
- Filesystem paths: always `pathlib.Path`. No `os.path`, `os.sep`, or string concatenation.
- No preemptive guardrails — don't add validation/warnings for parameters that are only wrong in the current environment. Trust the caller.
- Module / package names: `lowercase_with_underscores`. Test files: `test_*.py`. Service class names stay short: `OSDoc`, `OSIndex`, `OSSearch`, `MinioObject`.

## Testing

Tests use the standard library `unittest` + `unittest.mock`. Never require a live OpenSearch cluster or live MinIO — mock the client (for `ops_store`, patch `ops_store.base._opensearch_class` or `_bulk_helper`).

Name tests by behavior, e.g. `test_bulk_index_builds_actions`. Cover both return values and key client call kwargs.

## Commits & pull requests

Short, imperative, lowercase commit subjects (`add put_dataframe to minio_handler`). One change per commit.

Pull requests should include:

- a short summary of the change
- affected modules
- test commands run and their result

## Configuration

Load secrets and connection settings from environment variables. Don't commit credentials. See `CLAUDE.md` for the full env-var surface (`OPENSEARCH_*`, MinIO config, `AIRFLOW_MGMT_*`).
