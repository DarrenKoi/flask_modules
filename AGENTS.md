# Repository Guidelines

## Project Structure & Module Organization
This repository is a small Flask service with a reusable OpenSearch helper package.

- `index.py`: local app entrypoint
- `api/`: Flask blueprint registration and HTTP routes
- `config.py`: Flask configuration from environment variables
- `ops_store/`: class-based OpenSearch helpers (`base.py`, `document.py`, `index.py`, `search.py`, `logging.py`)
- `tests/`: unit tests for `ops_store`
- `README.md`: local run and usage notes
- `logs/`: runtime log output, ignored by git

Keep new web-facing code in `api/` and reusable search/storage logic in `ops_store/`.

## Build, Test, and Development Commands
- `python3.11 -m venv .venv && source .venv/bin/activate`: create and activate a local environment
- `pip install -r requirements.txt`: install Flask and `opensearch-py`
- `python index.py`: run the Flask app locally on port `8000` by default
- `python3 -m unittest discover -s tests -v`: run the test suite
- `python3 -m compileall ops_store tests`: quick import/syntax validation

Use `index:app` as the WSGI callable for deployments.

## Coding Style & Naming Conventions
Use 4-space indentation and standard Python typing. Follow the existing style:

- modules and packages: lowercase with underscores
- tests: `test_*.py`
- OpenSearch service classes: short names such as `OSDoc`, `OSIndex`, `OSSearch`

Keep functions small and direct. Prefer explicit imports and avoid `from __future__ import annotations` in this repository.

## Testing Guidelines
Tests use the standard library `unittest` framework with `unittest.mock` for client isolation. Add unit tests for new `ops_store` behavior and mock external OpenSearch calls rather than requiring a live cluster.

Name test cases by behavior, for example `test_bulk_index_builds_actions`. Cover both return values and key client call arguments.

## Commit & Pull Request Guidelines
Current history uses short, imperative, lowercase commit subjects, for example `scaffold flask blueprint app`. Keep commit messages concise and focused on one change.

Pull requests should include:

- a short summary of the change
- affected modules or routes
- test commands run and their result
- sample request/response notes if API behavior changes

## Security & Configuration Tips
Load secrets and cluster settings from environment variables such as `OPENSEARCH_HOST`, `OPENSEARCH_USER`, and `OPENSEARCH_PASSWORD`. Do not commit credentials or generated log files under `logs/`.
