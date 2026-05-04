"""
FTP ingest topic.

DAGs:
- ingest_dag.py:        @task version (download runs on Airflow worker)
- ingest_kpo_dag.py:    KubernetesPodOperator version (download runs in a pod)

Both DAGs iterate over `sources.SOURCES` via dynamic task mapping —
adding a new FTP source is a config-only change.
"""
