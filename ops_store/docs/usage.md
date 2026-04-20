# Usage Guide

## What `ops_store` is

`ops_store` is a thin wrapper around `opensearch-py`. It does not hide
OpenSearch concepts such as indexes, mappings, queries, or raw result payloads.
Its job is to give you:

- consistent config loading from environment variables
- a small class per responsibility
- optional default-index handling
- reusable logging for OpenSearch operations

If you want a full abstraction over OpenSearch, this package is intentionally
not that. It is closer to a convenience layer around the official client.

## Recommended startup pattern

Configure logging once during application startup, then reuse service instances
or reuse a shared client.

```python
from ops_store import OSDoc, OSIndex, OSSearch, configure_logging

configure_logging(level="INFO")

index_service = OSIndex(index="articles")
doc_service = OSDoc(index="articles")
search_service = OSSearch(index="articles")
```

That pattern works because `OSBase` creates a client automatically when you do
not pass one.

If you want tighter control, create one shared client and inject it into
multiple services:

```python
from ops_store import OSDoc, OSIndex, OSSearch, create_client, load_config

config = load_config()
client = create_client(config=config)

index_service = OSIndex(client=client, config=config, index="articles")
doc_service = OSDoc(client=client, config=config, index="articles")
search_service = OSSearch(client=client, config=config, index="articles")
```

This is the better pattern when you want all services to share the same client
instance explicitly.

## Environment variables

`OSConfig.from_env()` and `load_config()` support these variables:

- `OPENSEARCH_HOST`
- `OPENSEARCH_PORT`
- `OPENSEARCH_USER`
- `OPENSEARCH_PASSWORD`
- `OPENSEARCH_USE_SSL`
- `OPENSEARCH_VERIFY_CERTS`
- `OPENSEARCH_SSL_SHOW_WARN`
- `OPENSEARCH_CA_CERTS`
- `OPENSEARCH_BULK_CHUNK`
- `OPENSEARCH_TIMEOUT`
- `OPENSEARCH_MAX_RETRIES`
- `OPENSEARCH_RETRY_ON_TIMEOUT`
- `OPENSEARCH_HTTP_COMPRESS`
- `OPENSEARCH_LOG_LEVEL`
- `OPENSEARCH_LOG_DIR`

Boolean values accept common forms such as `true`, `false`, `1`, `0`, `yes`,
and `no`.

Important rule:

- if you set `OPENSEARCH_USER`, you must also set `OPENSEARCH_PASSWORD`
- if you set `OPENSEARCH_PASSWORD`, you must also set `OPENSEARCH_USER`

Otherwise `OSConfig` raises `ValueError`.

## Index operations

Use `OSIndex` when you are managing index lifecycle and settings.

```python
from ops_store import OSIndex

index_service = OSIndex(index="articles")

if not index_service.exists():
    index_service.create(
        mappings={
            "properties": {
                "title": {"type": "text"},
                "tags": {"type": "keyword"},
                "embedding": {
                    "type": "knn_vector",
                    "dimension": 3,
                },
            }
        }
    )
```

Useful behaviors to know:

- `create()` fills in shard, replica, and refresh defaults unless you override
  them.
- `update_settings()` wraps your settings inside `{"index": settings}` for the
  OpenSearch API.
- `get_aliases()` returns all aliases if neither `index=` nor `default_index`
  is set.

## Document operations

Use `OSDoc` for single-document writes and bulk writes.

```python
from ops_store import OSDoc

doc_service = OSDoc(index="articles")

doc_service.index(
    {"title": "Hello", "tags": ["flask"]},
    doc_id="post-1",
    refresh="wait_for",
)

doc_service.update("post-1", {"title": "Hello again"})
doc_service.upsert("post-2", {"title": "Created if missing"})
doc_service.delete("post-2")
```

Method behavior:

- `index()` sends the document as the request body
- `update()` sends `{"doc": ...}` for a partial update
- `upsert()` sends `{"doc": ..., "doc_as_upsert": True}`
- `delete()` deletes by id

### Bulk writes

Use `bulk()` if you already know the raw bulk action structure.

```python
actions = [
    {"_op_type": "index", "_index": "articles", "_id": "1", "_source": {"title": "One"}},
    {"_op_type": "index", "_index": "articles", "_id": "2", "_source": {"title": "Two"}},
]

doc_service.bulk(actions, refresh=True)
```

Use `bulk_index()` if you have plain document dictionaries and want this package
to build the index actions for you.

```python
documents = [
    {"id": "1", "title": "One"},
    {"id": "2", "title": "Two"},
]

doc_service.bulk_index(documents, id_field="id", refresh=True)
```

Important detail:

- `bulk_index()` expects a sequence, not a generator, because it logs
  `document_count=len(documents)`. Pass a list or tuple.

## Search operations

Use `OSSearch` for small, explicit query helpers.

```python
from ops_store import OSSearch

search_service = OSSearch(index="articles")

search_service.match("title", "flask", size=5)
search_service.term("status", "published")
search_service.multi_match("vector search", ["title", "body"])
```

Boolean queries:

```python
search_service.bool(
    must=[{"match": {"title": "flask"}}],
    filter=[{"term": {"status": "published"}}],
    size=20,
)
```

Vector search:

```python
search_service.knn(
    field="embedding",
    vector=[0.1, 0.2, 0.3],
    k=5,
    size=5,
)
```

Hybrid search:

```python
search_service.hybrid(
    query="flask tutorial",
    text_field="title",
    vector_field="embedding",
    vector=[0.1, 0.2, 0.3],
    k=5,
    size=10,
)
```

Important detail:

- `hybrid()` is a simple boolean `should` query combining `match` and `knn`
  clauses. It is not a more advanced ranking or fusion pipeline.

## Logging behavior

The package logs operation summaries, not full OpenSearch responses.

Recommended setup:

```python
from ops_store import configure_logging

configure_logging(level="INFO")
```

Default behavior after `configure_logging()`:

- logger name starts with `opensearch`
- logs propagate to the parent logger
- a file handler is added by default
- log files are written under `logs/opensearch`
- each process writes its own file, such as `opensearch.<pid>.log`

If you want direct console output in a script, enable `add_handler=True`.

```python
configure_logging(level="DEBUG", add_handler=True, propagate=False)
```

## Proper usage rules

These are the rules that matter most when using this package correctly:

- Configure logging once at startup instead of repeatedly in request handlers.
- Reuse a client or service objects when possible instead of recreating them for
  every operation.
- Set a default index at service construction time if most operations target the
  same index.
- If you do not set a default index, always pass `index=...` to avoid
  `ValueError`.
- Use `bulk()` only for prebuilt actions and `bulk_index()` for plain document
  dictionaries.
- Make sure vector fields are mapped correctly before calling `knn()` or
  `hybrid()`.
- Inject a mocked `client` in tests instead of requiring a live OpenSearch
  cluster.
- Do not pass client override kwargs when you already supplied `client=...`;
  `OSBase` rejects that combination.

## Example in a Flask service

This repository's Flask app currently exposes only health routes, but if you
want to use `ops_store` from a Flask handler, keep the route thin and move the
OpenSearch logic into the service classes:

```python
from flask import jsonify
from ops_store import OSDoc, configure_logging

configure_logging(level="INFO")
doc_service = OSDoc(index="articles")


def get_article(article_id: str):
    result = doc_service.get(article_id)
    return jsonify(result)
```

That keeps Flask responsible for HTTP and `ops_store` responsible for
OpenSearch-specific behavior.
