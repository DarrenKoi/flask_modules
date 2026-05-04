"""
ftp_ingest / ingest_dag — @task version.

One @task per source: pull file from FTP → upload to MinIO → cleanup.
Fanned out via .expand() over the SOURCES registry — adding a new source
= appending one dict to ftp_ingest/sources.py; no DAG change.

Use this version when:
- The download is light (small files, few hundred sources)
- Airflow worker has spare capacity
- You don't need pod-level isolation

Otherwise prefer ingest_kpo_dag.py.

Imports resolve because Airflow puts dags_folder on sys.path:
- `ftp_ingest` and `util.minio_handler` are both packages under dags/
- absolute imports only — no `from .sources import ...`

MinIO config: MinioObject() with no args reads MINIO_ENDPOINT,
MINIO_ACCESS_KEY, MINIO_SECRET_KEY from the worker's environment
(see util/minio_handler/base.py). Set those once on the platform; the
DAG only specifies the bucket and key.
"""

import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from platform import system


def _root_dir() -> Path:
    if "/opt/airflow/" in str(Path(__file__).resolve()):
        return Path("/opt/airflow/dags/airflow_repo.git/skewnono-scheduler1/dags")
    name = system()
    if name == "Windows":
        return Path("F:/skewnono")
    if name == "Linux":
        return Path("/project/workSpace")
    return Path(__file__).resolve().parents[1]


ROOT_DIR = _root_dir()
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from airflow.hooks.base import BaseHook  # noqa: E402
from airflow.sdk import dag, task  # noqa: E402

from ftp_ingest.lib.downloader import download_to_path  # noqa: E402
from ftp_ingest.sources import SOURCES, FtpSource  # noqa: E402
from util.minio_handler import MinioObject  # noqa: E402


MINIO_BUCKET = "raw-ingest"          # promote to an Airflow Variable if it varies per env

DEFAULT_ARGS = {
    "owner": "data-team",
    "retries": 3,
    "retry_delay": timedelta(minutes=2),
    "execution_timeout": timedelta(minutes=15),
}


@dag(
    dag_id="ftp_ingest_worker",
    description="Pull files from N FTP servers, upload to MinIO — runs on Airflow worker",
    start_date=datetime(2026, 1, 1),
    schedule="0 4 * * *",
    catchup=False,
    max_active_runs=1,
    default_args=DEFAULT_ARGS,
    tags=["ftp", "ingest", "worker"],
)
def ftp_ingest_worker():
    @task
    def download_and_upload(source: FtpSource) -> dict:
        conn = BaseHook.get_connection(source["conn_id"])

        # TemporaryDirectory cleans up on every exit path — success, exception,
        # or worker SIGTERM mid-task. Replaces a manual unlink that only ran on
        # the happy path.
        with tempfile.TemporaryDirectory(prefix=f"ftp_ingest_{source['name']}_") as tmp:
            local_path = Path(tmp) / Path(source["remote_path"]).name

            download_meta = download_to_path(
                host=conn.host,
                user=conn.login,
                password=conn.password,
                port=conn.port or 21,
                remote_path=source["remote_path"],
                local_path=local_path,
            )

            storage = MinioObject(bucket=MINIO_BUCKET)
            storage.upload(key=source["s3_key"], file_path=local_path)

            return {
                **download_meta,
                "bucket": MINIO_BUCKET,
                "s3_key": source["s3_key"],
            }

    download_and_upload.expand(source=SOURCES)


ftp_ingest_worker()
