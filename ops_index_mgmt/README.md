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

The cluster host defaults to `skewnono-db1-os.osp01.skhynix.com` and the user
defaults to `skewnono001`. The password is read from the environment and is not
stored in this repository.

```bash
export SKEWNONO_OPENSEARCH_PASSWORD="..."
python -m ops_index_mgmt.hitachi_sem_msr_info --dry-run
python -m ops_index_mgmt.hitachi_sem_msr_info
```

Optional overrides:

```bash
export SKEWNONO_OPENSEARCH_HOST="skewnono-db1-os.osp01.skhynix.com"
export SKEWNONO_OPENSEARCH_USER="skewnono001"
```

The aliases `cdsem_msr_info` and `hvsem_msr_info` should be used by ingest and
query code. OpenSearch writes to the current backing index through each alias
and rolls them to `cdsem_msr_info-000002`, `hvsem_msr_info-000002`, and so on.

Retention is index-age based. It removes whole backing indices after 50 days,
so exact document-level expiry depends on how much time each backing index spans.
