# ops_index_mgmt

Operational OpenSearch index setup scripts.

## SEM MSR info indices

`hitachi_sem_msr_info.py` creates:

- shared ISM policy `sem_msr_info_retention_policy`
- index template `cdsem_msr_info_template`
- index template `hvsem_msr_info_template`
- first backing index `cdsem_msr_info-000001`
- first backing index `hvsem_msr_info-000001`
- write/search alias `cdsem_msr_info`
- write/search alias `hvsem_msr_info`

Settings:

- primary shards: `3`
- replicas: `1`
- rollover: `15gb` total primary shard storage
- retention: delete backing indices after `50d`

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

The aliases `cdsem_msr_info` and `hvsem_msr_info` should be used by ingest and
query code. OpenSearch writes to the current backing index through each alias
and rolls them to `cdsem_msr_info-000002`, `hvsem_msr_info-000002`, and so on.

Retention is index-age based. It removes whole backing indices after 50 days,
so exact document-level expiry depends on how much time each backing index spans.
