"""Every 30 min: pull files from the equipment FTP fleet → MinIO + OpenSearch.

Thin orchestration only. All real logic lives in airflow-free helpers:
  - utils.ftp_fleet_downloader  — concurrent in-memory FTP downloader
  - utils.eqp_ftp_collect       — archive → parse → index glue

Design decisions baked in (see docs/ftp_fleet_downloader.md):
  - to_thread + ftplib (no aioftp, no PythonVirtualenvOperator).
  - In-memory streaming via on_file: peak RAM ~ concurrency x file size.
  - Tight per-host timeouts; one dead tool never blocks the fleet.
  - Threshold-based failure: green on normal partial failures, red only when
    a systemic problem (too many hosts down, or zero successes) shows up.
  - max_active_runs=1 + catchup=False + dagrun_timeout < 30 min so runs can't
    pile up or overlap.

Runtime inputs (NOT in source):
  - Airflow Variable  ``eqp_ftp_fleet``  — JSON list of host/file/listing specs.
  - Airflow Connection ``eqp_ftp``       — shared FTP login/password/port.

NOTE: the OpenSearch step imports ops_store, which is NOT vendored under
airflow_mgmt/ (only minio_handler is). Vendor ops_store the same way, or make
the repo root importable on the worker, before this DAG's index step can run.
The DAG itself parses fine without it (the import is deferred into the task).
"""

import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

from airflow.sdk import DAG, task

# ── sys.path bootstrap ──────────────────────────────────────────────────────
ROOT_DIR = Path(
    os.getenv("AIRFLOW_MGMT_ROOT")
    or next(
        (str(p) for p in Path(__file__).resolve().parents if (p / "project_root.txt").is_file()),
        "",
    )
).resolve()
if not ROOT_DIR.is_dir():
    raise RuntimeError("Cannot find airflow_mgmt root. Set AIRFLOW_MGMT_ROOT.")
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))
# ────────────────────────────────────────────────────────────────────────────

from ftp_handler.eqp_ftp_collect import build_host_specs, collect_fleet  # noqa: E402

# Operational knobs with env overrides — tuned for ~200 small-file hosts.
FLEET_VARIABLE = os.getenv("EQP_FTP_FLEET_VARIABLE", "eqp_ftp_fleet")
FTP_CONN_ID = os.getenv("EQP_FTP_CONN_ID", "eqp_ftp")
MINIO_BUCKET = os.getenv("EQP_FTP_BUCKET", "eqp-logs")
OPENSEARCH_INDEX = os.getenv("EQP_FTP_INDEX", "eqp_meas")
MAX_CONCURRENCY = int(os.getenv("EQP_FTP_MAX_CONCURRENCY", "48"))
CONNECT_TIMEOUT = float(os.getenv("EQP_FTP_CONNECT_TIMEOUT", "8"))
HOST_TIMEOUT = float(os.getenv("EQP_FTP_HOST_TIMEOUT", "60"))
# Fail (alert) only when more than this fraction of hosts/files fail.
FAILURE_THRESHOLD = float(os.getenv("EQP_FTP_FAILURE_THRESHOLD", "0.2"))


def parse_records(host: str, remote_path: str, data: bytes) -> list[dict]:
    """YOUR processing seam — turn raw file bytes into OpenSearch documents.

    Replace this stub. Branch on remote_path / file type, decode, and build
    docs with a deterministic ``_id`` so re-runs are idempotent. ``minio_key``
    is added by collect_fleet, so you don't set it here. Return [] to index
    nothing for a file.
    """
    raise NotImplementedError("implement parse_records for your file formats")


with DAG(
    dag_id="eqp_ftp_collector",
    description="Pull equipment FTP files into MinIO + OpenSearch every 30 minutes",
    start_date=datetime(2026, 1, 1),
    schedule="*/30 * * * *",
    catchup=False,
    max_active_runs=1,
    dagrun_timeout=timedelta(minutes=25),
    tags=["eqp", "ftp", "opensearch"],
) as dag:

    @task
    def collect() -> dict:
        import logging

        from airflow.hooks.base import BaseHook
        from airflow.sdk import Variable

        # Deferred imports: these run on the worker, not at DAG parse time.
        from minio_handler import MinioObject
        from ops_store import OSDoc  # requires ops_store on the worker (see module docstring)

        log = logging.getLogger(__name__)

        fleet = Variable.get(FLEET_VARIABLE, deserialize_json=True)
        specs = build_host_specs(fleet)

        conn = BaseHook.get_connection(FTP_CONN_ID)
        port = conn.port or 21

        # Clients built once and closed over by the callbacks. Both minio-py
        # and opensearch-py clients are thread-safe (pooled), so sharing them
        # across the concurrent on_file callbacks is fine.
        storage = MinioObject(bucket=MINIO_BUCKET)
        doc = OSDoc()

        def archive(host: str, remote_path: str, data: bytes) -> str:
            key = f"{host}/{remote_path.lstrip('/')}"
            storage.put(key, data)
            return key

        def index(docs: list[dict]) -> None:
            doc.bulk_index(OPENSEARCH_INDEX, docs)

        report = collect_fleet(
            specs,
            user=conn.login,
            password=conn.password,
            archive=archive,
            parse=parse_records,
            index=index,
            port=port,
            max_concurrency=MAX_CONCURRENCY,
            connect_timeout=CONNECT_TIMEOUT,
            host_timeout=HOST_TIMEOUT,
        )

        # Per-host/file failures are expected (equipment goes offline); log
        # them but only fail the task on a systemic problem.
        for failure in report.failures:
            log.warning("FTP collect failure: %s", failure)

        summary = {
            "hosts": len(specs),
            "ok": report.ok,
            "ng": report.ng,
            "failure_ratio": round(report.failure_ratio, 3),
        }
        log.info("eqp_ftp_collector summary: %s", summary)

        if report.ok == 0 or report.failure_ratio > FAILURE_THRESHOLD:
            raise RuntimeError(
                f"eqp_ftp_collector systemic failure: {summary} "
                f"(threshold={FAILURE_THRESHOLD})"
            )
        return summary

    collect()
