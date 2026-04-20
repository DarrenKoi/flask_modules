# Codebase Guide

## What this repository contains

This repository has two distinct parts:

1. A very small Flask app used as the HTTP entrypoint.
2. A reusable `ops_store` package that wraps `opensearch-py` with a small,
   class-based interface.

The Flask app is intentionally thin. Most of the reusable behavior lives in
`ops_store/`.

## Repository map

- `index.py`: top-level process entrypoint; creates the Flask app and runs it
  locally.
- `config.py`: Flask configuration loaded from environment variables.
- `api/__init__.py`: application factory and root `/` route registration.
- `api/routes.py`: API blueprint routes under `/api`.
- `ops_store/base.py`: connection config, client factory, and shared base class.
- `ops_store/document.py`: document CRUD and bulk write helpers.
- `ops_store/index.py`: index settings, mappings, refresh, and alias helpers.
- `ops_store/search.py`: query helpers for raw, lexical, boolean, vector, and
  aggregation searches.
- `ops_store/logging.py`: package logging configuration and result summarizing.
- `tests/test_ops_store_services.py`: unit tests for config loading, logging,
  CRUD helpers, and search query construction.

## Runtime flow

The request and service flow is simple:

1. `index.py` imports `create_app()` from `api`.
2. `api.create_app()` builds the Flask app, loads `Config`, and registers the
   `/api` blueprint.
3. Route functions in `api/routes.py` return JSON responses.
4. When application code wants OpenSearch access, it uses `ops_store`.
5. `ops_store.base.load_config()` reads connection settings from environment
   variables into `OSConfig`.
6. `ops_store.base.create_client()` turns `OSConfig` into an `OpenSearch`
   client.
7. `OSDoc`, `OSIndex`, and `OSSearch` inherit from `OSBase`, share the client
   lifecycle rules, and log summarized operation results through
   `ops_store.logging.log_result()`.

## `ops_store` module responsibilities

### `base.py`

`base.py` is the foundation of the package.

- `OSConfig` is a dataclass for OpenSearch connection settings.
- `OSConfig.from_env()` reads all supported `OPENSEARCH_*` environment
  variables.
- `create_client()` instantiates the actual `opensearchpy.OpenSearch` client.
- `OSBase` stores the client, optional config, and an optional default index.

Important behavior in this module:

- If `user` is set, `password` must also be set, and vice versa.
- `use_ssl=True` changes the host scheme to `https`.
- `OSBase._resolve_index()` raises `ValueError` if neither an explicit `index`
  argument nor `default_index` is available.
- If you pass an already-created `client`, you cannot also pass client override
  keyword arguments. That is enforced to avoid ambiguous configuration.

### `document.py`

`OSDoc` handles document-level writes and reads:

- `index()`: create or replace a single document
- `get()`: fetch a document by id
- `update()`: partial update using `{"doc": ...}`
- `upsert()`: update or insert using `doc_as_upsert=True`
- `delete()`: delete a document by id
- `bulk()`: send raw bulk actions
- `bulk_index()`: convert plain document dictionaries into bulk index actions

`bulk_index()` is intentionally higher level than `bulk()`. It expects a
sequence of document mappings and can pull `_id` from a document field when
`id_field` is set.

### `index.py`

`OSIndex` wraps index-management operations through `client.indices`.

- `exists()`: check whether an index exists
- `create()`: create an index with optional mappings and settings
- `delete()`: delete an index
- `get_settings()`: inspect current index settings
- `get_mapping()`: inspect mappings
- `update_settings()`: change index settings
- `refresh()`: refresh the index
- `get_aliases()`: get aliases for a specific index or all aliases if no index
  is resolved
- `update_aliases()`: submit alias add/remove actions

The `create()` method applies default settings unless you override them:

- `number_of_shards=1`
- `number_of_replicas=0`
- `refresh_interval="30s"`

### `search.py`

`OSSearch` wraps query patterns but still returns the raw OpenSearch response.
That makes it lightweight and predictable.

- `search_raw()`: send a raw search body directly
- `count()`: count matching documents
- `match()`: match query for a single field
- `term()`: exact-value term query
- `bool()`: boolean query builder
- `multi_match()`: multi-field full-text query
- `knn()`: vector k-NN query
- `hybrid()`: simple lexical plus vector `should` query
- `aggregate()`: search with aggregations

The package does not define its own response model. It leaves the response in
OpenSearch format so the caller can decide how much to extract from `hits`,
`aggregations`, and metadata.

### `logging.py`

`logging.py` provides package-specific logging helpers.

- `configure_logging()` sets up the logger once.
- `PIDFileHandler` writes logs to a file named with the current process id.
- `summarize_result()` trims large OpenSearch responses into smaller summaries.
- `log_result()` writes a structured log payload and then returns the original
  result unchanged.

One important detail: the package starts with a `NullHandler`, so importing
`ops_store` does not produce logs by itself. Logging becomes visible only after
`configure_logging()` is called or the surrounding application wires handlers in
another way.

## What the tests verify

`tests/test_ops_store_services.py` covers the key package behaviors:

- environment-driven config loading
- client creation arguments
- logging setup and file output
- bulk action construction
- query body generation
- default index settings for index creation

The tests use `unittest.mock` instead of a live OpenSearch cluster, which is
the right fit for this package because the package is mostly argument shaping
and delegation around the OpenSearch client.
