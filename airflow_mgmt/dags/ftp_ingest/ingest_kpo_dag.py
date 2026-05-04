"""
ftp_ingest / ingest_kpo_dag — KubernetesPodOperator version.

Same shape as ingest_dag.py, but each download runs in its own pod.

Why pods:
- Resource isolation (per-task CPU/memory limits)
- The download dependencies live in the image, not in Airflow's worker
- Heavy/parallel work scales on the K8s cluster, not the worker count
- The image is the deployment artifact — versioned, scanned, reproducible

The image (`your-registry/ftp-downloader:1.0`) bundles:
- ftp_ingest/lib/downloader.py
- a thin CLI: `python -m ftp_downloader_cli --remote-path X --s3-key Y`
The CLI source lives in your image-build repo, NOT here. This repo only
contains DAG definitions.

Credentials note: FTP host/user/password come from a Kubernetes Secret
mounted into the pod (provisioned by the platform team), NOT from this
DAG. The DAG only passes non-secret config (remote path, destination key).
"""

from datetime import datetime, timedelta

from airflow.providers.cncf.kubernetes.operators.pod import KubernetesPodOperator
from airflow.sdk import dag

from ftp_ingest.sources import SOURCES, FtpSource


def _kpo_kwargs_for(source: FtpSource) -> dict:
    """Per-task overrides — everything that varies between the 200 sources."""
    return {
        "task_id": f"download_{source['name']}",
        "arguments": [
            "--remote-path", source["remote_path"],
            "--s3-key", source["s3_key"],
            "--source-name", source["name"],
        ],
    }


DEFAULT_ARGS = {
    "owner": "data-team",
    "retries": 3,
    "retry_delay": timedelta(minutes=2),
    "execution_timeout": timedelta(minutes=15),
}


@dag(
    dag_id="ftp_ingest_kpo",
    description="Pull files from N FTP servers — each in its own Kubernetes pod",
    start_date=datetime(2026, 1, 1),
    schedule="0 4 * * *",
    catchup=False,
    max_active_runs=1,
    default_args=DEFAULT_ARGS,
    tags=["ftp", "ingest", "kpo"],
)
def ftp_ingest_kpo():
    # .partial(...) → fields shared by every task instance.
    # .expand_kwargs(...) → fields that vary per source (see _kpo_kwargs_for).
    KubernetesPodOperator.partial(
        namespace="data-pipelines",
        image="your-registry/ftp-downloader:1.0",
        cmds=["python", "-m", "ftp_downloader_cli"],
        get_logs=True,
        # Resource limits, secret mounts, service account, node selector,
        # and security context all go here. Ask your platform team for
        # the standard set of fields to fill in.
    ).expand_kwargs([_kpo_kwargs_for(s) for s in SOURCES])


ftp_ingest_kpo()
