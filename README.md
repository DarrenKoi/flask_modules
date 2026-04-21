# flask_modules

Flask project skeleton for Python 3.11 with:

- root `index.py` entrypoint
- `api/` package for Blueprints
- root `wsgi.ini` for WSGI/uWSGI-style cloud deployments
- `ops_store/` package with class-based OpenSearch helpers

## Run locally

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python index.py
```

## Flask Logging

For application-wide file logging outside `ops_store`, use the root-level
`logging_config.py` helper.

```python
from flask import Flask

from logging_config import configure_flask_logging

app = Flask(__name__)
configure_flask_logging(app, log_dir="logs/flask", log_name="server")
```

That creates `logs/flask/server.log`, rotates it at midnight, and keeps the
latest three rotated log files by default. If you want to target the root or a
named logger instead of `app.logger`, use `configure_logging(...)`.

For older code that already expects `setup_logger(path_dir, name)`, that
compatibility entrypoint is also available and uses `name` as both the logger
name and log filename base.

## OpenSearch Helpers

The project includes a class-based `ops_store` package built on top of
`opensearch-py`.

```python
from ops_store import OSDoc, OSIndex, OSSearch

index_crud = OSIndex(index="articles")
document_crud = OSDoc(index="articles")
search_service = OSSearch(index="articles")

index_crud.create(
    mappings={
        "properties": {
            "title": {"type": "text"},
            "tags": {"type": "keyword"},
        }
    }
)

document_crud.index({"title": "Hello", "tags": ["flask"]}, doc_id="post-1")
document_crud.upsert("post-2", {"title": "Updated later"})
result = search_service.match("title", "Hello")
```

`ops_store` does not log OpenSearch calls itself. Observe the cluster through
OpenSearch/Kibana dashboards or a dedicated monitoring service. For Flask
application logs, use the root-level `logging_config.py` helper.

Connection settings are read from environment variables such as
`OPENSEARCH_HOST`, `OPENSEARCH_PORT`, `OPENSEARCH_USER`,
`OPENSEARCH_PASSWORD`, and `OPENSEARCH_USE_SSL`.

## Routes

- `GET /`
- `GET /api/health`
- `GET /api/ping`

## Cloud entrypoint

Use `index:app` as the WSGI callable.
