# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Common commands

```bash
python3.11 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Run the Flask app locally (defaults to 0.0.0.0:8000)
python index.py

# Run the full test suite
python3 -m unittest discover -s tests -v

# Run a single test case or method
python3 -m unittest tests.test_ops_store_services.OSDocTests -v
python3 -m unittest tests.test_ops_store_services.OSDocTests.test_bulk_index_builds_actions -v

# Quick import/syntax validation (no linter configured)
python3 -m compileall ops_store tests
```

WSGI callable for cloud/uWSGI deployment: `index:app` (see `wsgi.ini`).

## Architecture

Two independent concerns live side by side; keep them separated.

**Web layer (`api/` + `index.py` + `config.py`)** — `index.py` calls `api.create_app()`, which builds the Flask app, loads `config.Config` (env-driven), registers the `api_bp` blueprint at `/api`, and attaches a root `/` handler. New HTTP routes go on `api_bp` in `api/routes.py`. Flask config fields all come from env vars (`FLASK_DEBUG`, `FLASK_TESTING`, `SECRET_KEY`, `FLASK_SERVER_NAME`).

**OpenSearch layer (`ops_store/`)** — a reusable, class-based wrapper over `opensearch-py`. The shape to preserve:

- `OSConfig` (dataclass, `slots=True`) holds all connection settings and has `from_env()` / `to_client_kwargs()`. New settings should be added here, not sprinkled across services.
- `create_client()` / `load_config()` are the entry points; tests patch `ops_store.base._opensearch_class` to avoid a real cluster.
- `OSBase` is the single base class for every service. It owns `self.client`, `self.config`, `self.default_index`, and a scoped `self.logger` (`opensearch.<classname>`). Every service method resolves the index via `self._resolve_index(index)` and pipes its return value through `self._log_result(action, result, **context)` — `log_result` calls `summarize_result` to flatten OpenSearch payloads before logging. Follow this pattern for any new service method so results stay observable without `print`.
- Three concrete services on top of `OSBase`: `OSDoc` (single + bulk doc CRUD, uses `opensearchpy.helpers.bulk` via the `_bulk_helper` indirection so tests can mock it), `OSIndex` (index/alias management), `OSSearch` (lexical, kNN, hybrid, aggregations — all delegate to `search_raw`).
- Package `__init__.py` is the public API; add new exports there and in `__all__`.

**Logging (`ops_store/logging.py`)** — loggers live under the `opensearch` namespace. `configure_logging()` defaults to `propagate=True` and installs a `PIDFileHandler` that writes one file per process PID under `logs/opensearch/` (env override: `OPENSEARCH_LOG_DIR`, level: `OPENSEARCH_LOG_LEVEL`). This per-PID design matters for multi-worker Flask/uWSGI deployments — do not replace it with a single `FileHandler` without considering worker concurrency. Set `add_handler=True` only for standalone scripts that also want console output.

## Conventions (from AGENTS.md — enforce these)

- Python 3.11 features are expected: `Self`, PEP 604 unions (`str | None`), `slots=True` dataclasses.
- Do **not** use `from __future__ import annotations` in this repo.
- 4-space indent, explicit imports, small direct functions.
- Module/package names: lowercase_with_underscores. Test files: `test_*.py`. Service class names stay short: `OSDoc`, `OSIndex`, `OSSearch`.
- Tests use stdlib `unittest` + `unittest.mock`. Never require a live OpenSearch cluster — mock the client or patch `_opensearch_class`/`_bulk_helper`. Name tests by behavior (`test_bulk_index_builds_actions`); assert both return values and key client call kwargs.
- Commits: short, imperative, lowercase (`scaffold flask blueprint app`).

## Configuration surface

Env vars read by the code — keep this list in sync if you add new ones:

- Flask: `FLASK_HOST`, `FLASK_PORT`, `FLASK_DEBUG`, `FLASK_TESTING`, `FLASK_SERVER_NAME`, `SECRET_KEY`
- OpenSearch connection: `OPENSEARCH_HOST`, `OPENSEARCH_PORT`, `OPENSEARCH_USER`, `OPENSEARCH_PASSWORD`, `OPENSEARCH_USE_SSL`, `OPENSEARCH_VERIFY_CERTS`, `OPENSEARCH_SSL_SHOW_WARN`, `OPENSEARCH_CA_CERTS`
- OpenSearch tuning: `OPENSEARCH_BULK_CHUNK`, `OPENSEARCH_TIMEOUT`, `OPENSEARCH_MAX_RETRIES`, `OPENSEARCH_RETRY_ON_TIMEOUT`, `OPENSEARCH_HTTP_COMPRESS`
- Logging: `OPENSEARCH_LOG_LEVEL`, `OPENSEARCH_LOG_DIR`
