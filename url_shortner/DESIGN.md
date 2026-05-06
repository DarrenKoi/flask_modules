# `url_shortner/` — design notes

Internal URL shortener service for company employees. Backed by MongoDB
(durable mapping store), Redis (hot cache), OpenSearch (click analytics
via `ops_store`), and a Flask HTTP layer.

## Why this exists

Employees need a way to turn long internal links (wiki pages, dashboards,
Confluence trees, signed S3 URLs) into short, memorable codes they can
paste into chat, email, or printed materials. Because the service runs
inside the corporate network, abuse mitigation (rate limiting,
blocklists, interstitials) is intentionally out of scope.

The package follows the same scaffold as `ops_store/` and
`minio_handler/`: `slots=True` dataclass config → `from_env()` →
`create_client()` → `*Base` service → domain service classes → public
`__all__` in `__init__.py`.

## Design decisions

| Decision | Choice | Why |
|---|---|---|
| User-facing surface | JSON API + minimal HTML form | Employees can paste in a browser; tools can POST JSON |
| Code generation | Random 7-char base62 with collision retry | Fixed-length, unguessable; collisions vanishingly rare at internal scale |
| Custom aliases | Allowed, share namespace with auto-generated | Mongo's unique `_id` index enforces uniqueness atomically |
| Analytics path | Compose `ops_store.OSDoc` | Reuse existing connection logic; avoid duplicating `opensearch-py` setup |
| Click logging | Fire-and-forget (swallows errors) | Analytics outage must never break redirects |
| Abuse mitigation | None | Internal deployment, trusted users |

## Architecture

```
                 ┌────────────┐
   POST /shorten │            │  insert {_id: code, url, ...}
   ──────────────►   Flask    ├────────────────────► MongoDB (durable)
                 │            │  SET cache             ▲
                 │            ├────► Redis (hot)       │  unique index on _id
                 │            │                        │  enforces collisions
   GET /<code>   │            │  GET cache → fallback  │
   ──────────────►            ├────► Redis ──miss──► MongoDB
                 │            │
                 │            │  fire-and-forget click event
                 │            ├──────────────► ops_store.OSDoc.index
                 └────────────┘                 (OpenSearch click log)
```

Read path is ~1000× hotter than the write path: optimize for redirects.
Cache hit returns in microseconds; on miss, Mongo is hit and Redis is
repopulated. Click logging never blocks the redirect.

## Package layout

```
url_shortner/
├── __init__.py              # public API (__all__)
├── base.py                  # MongoConfig, RedisConfig, *Base classes, create_*_client
├── mapping.py               # URLMapping  — MongoDB CRUD on the mappings collection
├── cache.py                 # CacheLayer  — Redis read-through cache helpers
├── analytics.py             # ClickAnalytics — uses ops_store.OSDoc to log + aggregate
├── codegen.py               # generate_code(), is_valid_alias()
├── service.py               # ShortenerService — orchestrates the four pieces above
├── app.py                   # Flask app factory create_app()
├── templates/
│   └── index.html           # paste-URL form
└── DESIGN.md                # this file
```

Tests live under repo `tests/`:

```
tests/
├── test_url_shortner_codegen.py
├── test_url_shortner_mapping.py
├── test_url_shortner_cache.py
├── test_url_shortner_service.py
└── test_url_shortner_app.py
```

## Module responsibilities

### `base.py`

Two config dataclasses (`MongoConfig`, `RedisConfig`), each with a
`from_env()` classmethod and a `to_client_kwargs()` method. Two
module-level indirections (`_mongo_client_class()`,
`_redis_client_class()`) so tests can patch the client class without
reaching into `pymongo` or `redis`. Two factory functions
(`create_mongo_client()`, `create_redis_client()`) that take an optional
config plus override kwargs (using `dataclasses.replace()`). Two service
base classes (`MongoBase`, `RedisBase`) that own a client and resolve
default database/collection names — same shape as `OSBase` and
`MinioBase`.

### `codegen.py`

Two pure functions:

- `generate_code(length=7) -> str` — random base62.
- `is_valid_alias(alias) -> bool` — `^[A-Za-z0-9_-]{2,32}$`.

No I/O, no class, trivially testable.

### `mapping.py` — `URLMapping(MongoBase)`

- `create(code, url, owner=None, is_custom=False)` — inserts
  `{_id: code, url, owner, is_custom, created_at}`. Raises
  `pymongo.errors.DuplicateKeyError` on collision; the caller decides
  whether to retry or surface as `AliasTakenError`.
- `lookup(code)` — `find_one({"_id": code})`.
- `list_by_owner(owner, limit=100)` — sorted by `created_at` desc.
- `ensure_indexes()` — creates the `owner` and `created_at` indexes
  used by listing views. The unique index on `_id` is implicit.

### `cache.py` — `CacheLayer(RedisBase)`

- `get(code)` / `set(code, url, ttl=None)` / `invalidate(code)`.
- Key format: `urlshortner:code:{code}` (constant `KEY_PREFIX`).
- TTL defaults to `RedisConfig.default_ttl` (24h).
- Redis client is configured with `decode_responses=True`, so cache
  returns `str`, not `bytes`.

### `analytics.py` — `ClickAnalytics`

Holds an `ops_store.OSDoc` rather than inheriting from it (composition,
not inheritance — `OSDoc` already inherits from `OSBase`).

- `log_click(code, *, timestamp, ip, user_agent, referrer, owner)` —
  indexes a click event into the configured analytics index.
- `top_codes(window="7d", size=10)` — terms aggregation on `code`,
  filtered by `now-{window}`.
- Default index: `url_shortner_clicks` (env override:
  `URLSHORTNER_ANALYTICS_INDEX`).

### `service.py` — `ShortenerService`

The only place the three stores are coordinated.

- `shorten(url, *, alias=None, owner=None) -> str`:
  - With alias: validate, then `mapping.create(...)`. On
    `DuplicateKeyError`, raise `AliasTakenError`.
  - Without alias: loop up to `max_retries`, generating a fresh code
    each iteration, calling `mapping.create(...)`. On
    `DuplicateKeyError`, retry; on success, return the code.
- `resolve(code) -> str | None`:
  - Read-through cache: `cache.get()` → on miss, `mapping.lookup()` →
    on hit, `cache.set()` and return.
- `record_click(code, meta)`:
  - Delegates to `analytics.log_click`. **Swallows all exceptions** —
    analytics outages must not break redirects.

`DuplicateKeyError` is detected by class name rather than imported, so
tests can raise a stand-in exception and the service stays decoupled
from `pymongo.errors` at the type level.

### `app.py` — `create_app(*, service=None, base_url=None)`

Flask app factory. Accepts a pre-built `ShortenerService` for testing;
otherwise wires up real `URLMapping`, `CacheLayer`, and
`ClickAnalytics`.

Routes:

- `GET /` — renders `templates/index.html` (paste-URL form).
- `POST /shorten` — JSON body `{url, alias?, owner?}` → returns
  `{code, short_url}` with status 201. Status 400 for invalid alias or
  missing URL; status 409 for taken alias.
- `GET /<code>` — 302 redirect to the long URL; logs a click event
  fire-and-forget. 404 when unknown.

## Configuration surface

All env vars are also documented in repo-root `CLAUDE.md`.

- **MongoDB**: `MONGODB_HOST`, `MONGODB_PORT`, `MONGODB_USER`,
  `MONGODB_PASSWORD`, `MONGODB_DATABASE`, `MONGODB_COLLECTION`,
  `MONGODB_AUTH_SOURCE`, `MONGODB_TIMEOUT_MS`, `MONGODB_MAX_POOL_SIZE`.
- **Redis**: `REDIS_HOST`, `REDIS_PORT`, `REDIS_DB`, `REDIS_PASSWORD`,
  `REDIS_SSL`, `REDIS_TIMEOUT`, `REDIS_TTL`.
- **App**: `URLSHORTNER_BASE_URL` (origin used to render `short_url` in
  responses), `URLSHORTNER_ANALYTICS_INDEX` (default
  `url_shortner_clicks`).
- **Analytics OpenSearch cluster**: reuses the `OPENSEARCH_*` env vars
  consumed by `ops_store`.

## Testing

Stdlib `unittest` + `unittest.mock`. No live services required.

- `test_url_shortner_codegen.py` — pure-function tests on
  `generate_code` and `is_valid_alias`.
- `test_url_shortner_mapping.py` — patches `client[db][coll]` on a
  `MagicMock()` to assert `insert_one` / `find_one` / `find().sort()
  .limit()` / `create_index` call kwargs.
- `test_url_shortner_cache.py` — `Mock()` Redis client, asserts
  prefixed keys and TTL behavior.
- `test_url_shortner_service.py` — passes mocked `URLMapping`,
  `CacheLayer`, `ClickAnalytics` directly into `ShortenerService`.
  Verifies collision retry, max-retries exhaustion, alias-taken
  surfacing, read-through cache, fire-and-forget click logging.
- `test_url_shortner_app.py` — uses `app.test_client()` plus a
  `ShortenerService` whose dependencies are mocks; asserts status
  codes, JSON bodies, and redirect locations.

Run:

```
python -m unittest discover -s tests -v
python -m unittest tests.test_url_shortner_service -v
```

## Operational verification

End-to-end smoke test with all three stores reachable:

```
$env:FLASK_APP = "url_shortner.app:create_app"
flask run

# In another shell:
curl -X POST http://localhost:5000/shorten -H "Content-Type: application/json" \
     -d '{"url": "https://wiki.company.local/onboarding", "alias": "onboard"}'
# → {"code": "onboard", "short_url": "http://localhost:5000/onboard"}

curl -i http://localhost:5000/onboard
# → 302 Location: https://wiki.company.local/onboarding
```

Then check that an event landed in OpenSearch:

```
GET url_shortner_clicks/_search
{ "query": { "term": { "code": "onboard" } } }
```

## Out of scope (deferred work)

- **Authentication / SSO.** Add when integrating with the company
  identity provider; `owner` is already a first-class field on the
  mapping document.
- **Per-user rate limiting.** Not needed internally per current
  requirements.
- **Link expiration / soft delete.** Schema has room (no `deleted_at`
  field yet — add when the use case appears).
- **Bulk shorten (CSV upload).**
- **`/my-links` page** showing a user's own links and click counts.
  `URLMapping.list_by_owner` and `ClickAnalytics.top_codes` already
  support this; only the UI is missing.
