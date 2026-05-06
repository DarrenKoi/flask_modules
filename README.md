# flask_modules

Personal toolbox of work-supporting Python modules. The directory name is historical — there is no Flask app here. Each top-level package is independent and meant to be imported from notebooks, scripts, or Airflow tasks.

## Modules

- `ops_store/` — class-based `opensearch-py` wrapper (`OSDoc`, `OSIndex`, `OSSearch`).
- `minio_handler/` — class-based MinIO / S3-compatible client (`MinioObject`, presigned URLs, parquet round-trip, image helpers).
- `ops_index_mgmt/` — operational scripts that materialize specific OpenSearch indices on the company cluster.
- `airflow_mgmt/` — sandbox + production-bound code for the company Airflow 3.1.8 platform. See `airflow_mgmt/README.md`.

## Setup

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Tests

```bash
# Full suite
python3 -m unittest discover -s tests -v

# Single case
python3 -m unittest tests.test_ops_store_services.OSDocTests -v

# Quick syntax check
python3 -m compileall ops_store minio_handler ops_index_mgmt tests

# Airflow DAG integrity (no Airflow server needed)
python3 -m pytest airflow_mgmt/tests -v
```

## Quick reference — `ops_store`

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

document_crud.index({"title": "Hello", "tags": ["airflow"]}, doc_id="post-1")
document_crud.upsert("post-2", {"title": "Updated later"})
result = search_service.match("title", "Hello")
```

`OSIndex.exists()` treats either a concrete index name or an alias as an existing target by default. Pass `include_aliases=False` for index-only checks. `OSIndex.describe(name)` summarizes whether the name is an index or alias, its backing indices, attached aliases, and whether a rollover write alias is configured.

`ops_store` does not log its own calls — observe the cluster through OpenSearch / Kibana.

Connection settings come from env vars: `OPENSEARCH_HOST`, `OPENSEARCH_PORT`, `OPENSEARCH_USER`, `OPENSEARCH_PASSWORD`, `OPENSEARCH_USE_SSL`, and the rest listed in `CLAUDE.md`.
