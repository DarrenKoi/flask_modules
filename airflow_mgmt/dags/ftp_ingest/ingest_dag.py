"""
ftp_ingest / ingest_dag — @task version.

One @task definition, fanned out via .expand() over the SOURCES registry.
Airflow generates one task instance per source at runtime. Adding a new
source = appending one dict to ftp_ingest/sources.py; no DAG change.

Use this version when:
- The download is light (small files, few hundred sources)
- Airflow worker has spare capacity
- You don't need pod-level isolation

Otherwise prefer ingest_kpo_dag.py.

Imports resolve because Airflow puts dags_folder on sys.path:
- `ftp_ingest` is a package (dags/ftp_ingest/__init__.py exists)
- absolute imports only — no `from .sources import ...`
"""

import tempfile
from datetime import datetime, timedelta
from pathlib import Path

from airflow.hooks.base import BaseHook
from airflow.sdk import dag, task

from ftp_ingest.lib.downloader import download_to_path
from ftp_ingest.sources import SOURCES, FtpSource


DEFAULT_ARGS = {
    "owner": "data-team",
    "retries": 3,
    "retry_delay": timedelta(minutes=2),
    "execution_timeout": timedelta(minutes=15),
}


@dag(
    dag_id="ftp_ingest_worker",
    description="Pull files from N FTP servers in parallel — runs on Airflow worker",
    start_date=datetime(2026, 1, 1),
    schedule="0 4 * * *",
    catchup=False,
    max_active_runs=1,
    default_args=DEFAULT_ARGS,
    tags=["ftp", "ingest", "worker"],
)
def ftp_ingest_worker():
    @task
    def download(source: FtpSource) -> dict:
        conn = BaseHook.get_connection(source["conn_id"])
        local_path = (
            Path(tempfile.gettempdir())
            / "ftp_ingest"
            / source["name"]
            / Path(source["remote_path"]).name
        )
        return download_to_path(
            host=conn.host,
            user=conn.login,
            password=conn.password,
            port=conn.port or 21,
            remote_path=source["remote_path"],
            local_path=local_path,
        )

    download.expand(source=SOURCES)


ftp_ingest_worker()
