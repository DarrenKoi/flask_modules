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

## OpenSearch Helpers

The project includes a class-based `ops_store` package built on top of
`opensearch-py`.

```python
from ops_store import (
    OSDoc,
    OSIndex,
    OSSearch,
    configure_logging,
)

# Flask/FastAPI: let the app server handle handlers.
configure_logging(level="INFO")

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

Once logging is configured, CRUD and search calls log their result summaries
through the `opensearch` logger, so you do not need `print(...)` for normal
usage tracing.

By default, the package also writes log files under `logs/opensearch` at the
project root. Each worker process writes to its own file such as
`logs/opensearch/opensearch.<pid>.log`, which is safer for Flask/FastAPI
deployments using multiple workers.

For Flask/FastAPI, the logger still propagates to the application or server
logger by default. That avoids fighting the framework logging pipeline while
still preserving dedicated OpenSearch logs on disk. Use `add_handler=True`
only when you also want direct console output for standalone scripts or local
debugging.

Connection settings are read from environment variables such as
`OPENSEARCH_HOST`, `OPENSEARCH_PORT`, `OPENSEARCH_USER`,
`OPENSEARCH_PASSWORD`, and `OPENSEARCH_USE_SSL`.

## Routes

- `GET /`
- `GET /api/health`
- `GET /api/ping`

## Cloud entrypoint

Use `index:app` as the WSGI callable.
