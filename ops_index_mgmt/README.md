# ops_index_mgmt

Operational OpenSearch index setup scripts.

## SEM MSR info indices

`hitachi_sem_msr_info.py` creates:

- shared ISM policy `sem_meas_hist_retention_policy`
- index template `meas_hist_cdsem_template`
- index template `meas_hist_hvsem_template`
- first backing index `meas_hist_cdsem-000001`
- first backing index `meas_hist_hvsem-000001`
- write/search alias `meas_hist_cdsem`
- write/search alias `meas_hist_hvsem`

Settings:

- primary shards: `3`
- replicas: `1`
- rollover: backing index age reaches `60d`
- retention: delete backing indices after `365d`

Connection values are declared near the top of
`ops_index_mgmt/hitachi_sem_msr_info.py`:

```python
OPENSEARCH_HOST = "skewnono-db1-os.osp01.skhynix.com"
OPENSEARCH_USER = "skewnono001"
OPENSEARCH_PASSWORD = ""
```

Set `OPENSEARCH_PASSWORD` before running the script.

```bash
python -m ops_index_mgmt.hitachi_sem_msr_info --dry-run
python -m ops_index_mgmt.hitachi_sem_msr_info
```

The aliases `meas_hist_cdsem` and `meas_hist_hvsem` should be used by ingest and
query code. OpenSearch writes to the current backing index through each alias
and rolls them to `meas_hist_cdsem-000002`, `meas_hist_hvsem-000002`, and so on.

Rollover and retention are index-age based. The policy rolls each backing index
after 60 days and removes whole backing indices after 365 days, so exact
document-level expiry depends on how much time each backing index spans.

After the aliases exist, pandas DataFrames can be inserted through
`ops_store.OSDoc.bulk_index_dataframe()`:

```python
from ops_store import OSDoc

doc_service = OSDoc(client=client)

doc_service.bulk_index_dataframe(
    cdsem_df,
    index="meas_hist_cdsem",
    id_field="doc_id",
    op_type="create",
)
doc_service.bulk_index_dataframe(
    hvsem_df,
    index="meas_hist_hvsem",
    id_field="doc_id",
    op_type="create",
)
```

## Elasticsearch → OpenSearch reindex

`es_to_os_reindex.py` copies one ES index into one OpenSearch index using the
ES scroll API on the read side and `ops_store.OSDoc.bulk` on the write side.
Document ids are preserved, so re-runs overwrite rather than duplicate.

Set both clusters' connection consts at the top of the file (`ES_*` and
`OPENSEARCH_*`), then:

```bash
# Inspect what would happen — no cluster contact.
python -m ops_index_mgmt.es_to_os_reindex --dry-run \
    --source-index my_es_index --dest-index my_os_index

# Run the migration.
python -m ops_index_mgmt.es_to_os_reindex \
    --source-index my_es_index --dest-index my_os_index

# Filter with an ES query DSL fragment.
python -m ops_index_mgmt.es_to_os_reindex \
    --source-index my_es_index --dest-index my_os_index \
    --query '{"range":{"@timestamp":{"gte":"2026-01-01"}}}'
```

The destination index must already exist with the desired mapping/settings —
this script copies documents, not index metadata. Create the destination
index (or its template + write alias) ahead of time.

Requires the `elasticsearch` Python package on the source side in addition to
`opensearch-py` on the destination side. The source cluster is Elasticsearch
7.x, so pin the client to the matching major to avoid the 8.x product-check:

```bash
pip install "elasticsearch>=7,<8"
```
