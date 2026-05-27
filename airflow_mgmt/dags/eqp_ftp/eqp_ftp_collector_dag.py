"""Every 30 min: pull files from the equipment FTP fleet → MinIO + OpenSearch.

Thin orchestration only. All real logic lives in airflow-free helpers:
  - ftp_handler.direct_downloader.fleet_downloader  — concurrent FTP downloader
  - ftp_handler.direct_downloader.collect           — archive → parse → index glue

Why a virtualenv task:
  The index step builds ``ops_store.OSDoc``, which imports ``opensearchpy``.
  That library is NOT installed on the company workers (redis is; opensearch-py
  is not), so the collect step runs inside a ``PythonVirtualenvOperator`` that
  pip-installs ``opensearch-py`` per run from the internal Nexus mirror. When ops
  adds opensearch-py to the worker image, this can revert to a plain ``@task``.

  Consequences of the venv (see dag_templates/virtualenv_task_template.py):
  - The callable runs in a fresh subprocess: ALL imports and the sys.path
    bootstrap happen INSIDE it, and DAG-module globals are not visible there.
  - ``Variable.get`` / ``BaseHook.get_connection`` need Airflow runtime/DB
    access, which the venv subprocess lacks — so a normal ``load_config`` task
    resolves them first and passes plain values in via ``op_kwargs``.
  - ``minio`` / ``airflow`` come from ``system_site_packages=True``; only
    ``opensearch-py`` is pip-installed on top.

Design decisions baked in (see ftp_handler/docs/adr/ftp_fleet_downloader.md):
  - ThreadPoolExecutor + ftplib (no aioftp). Event-loop-free.
  - In-memory streaming via on_file: peak RAM ~ concurrency x file size.
  - Tight per-host timeouts; one dead tool never blocks the fleet.
  - Threshold-based failure: green on normal partial failures, red only when
    a systemic problem (too many hosts down, or zero successes) shows up.
  - max_active_runs=1 + catchup=False + dagrun_timeout < 30 min so runs can't
    pile up or overlap.

Runtime inputs (NOT in source):
  - Airflow Variable  ``eqp_ftp_fleet``  — JSON list of host/file/listing specs.
  - Airflow Connection ``eqp_ftp``       — shared FTP login/password/port.
  - Worker env: OPENSEARCH_* and MINIO_* (read by OSConfig / MinioConfig).
"""

import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

from airflow.providers.standard.operators.python import PythonVirtualenvOperator
from airflow.sdk import DAG, task

# ── Nexus PyPI (workers cannot reach public PyPI) ───────────────────────────
# Set before the operator builds its cached venv so uv/pip pull opensearch-py
# from the internal mirror. Trusted-host vars are space-separated. Mirrors the
# dag_templates/virtualenv_task_template.py setup.
NEXUS_PYPI_INDEX_URLS = [
    "http://nexus-sddc.datalake.skhynix.com/repository/pypi-group/simple",
    "http://nexus.skhynix.com:8081/repository/pypi-group/simple",
]
NEXUS_PYPI_HOSTS = [
    "nexus-sddc.datalake.skhynix.com",
    "nexus.skhynix.com",
]
os.environ.setdefault("UV_DEFAULT_INDEX", NEXUS_PYPI_INDEX_URLS[0])
os.environ.setdefault("UV_INDEX", " ".join(NEXUS_PYPI_INDEX_URLS[1:]))
os.environ.setdefault("UV_INSECURE_HOST", " ".join(NEXUS_PYPI_HOSTS))
os.environ.setdefault("PIP_INDEX_URL", NEXUS_PYPI_INDEX_URLS[0])
os.environ.setdefault("PIP_EXTRA_INDEX_URL", " ".join(NEXUS_PYPI_INDEX_URLS[1:]))
os.environ.setdefault("PIP_TRUSTED_HOST", " ".join(NEXUS_PYPI_HOSTS))


# ── sys.path bootstrap (parent process) ─────────────────────────────────────
# Only used here to compute REPO_ROOT, which is passed as a plain string into
# the venv callable (which redoes the insert itself — module globals don't
# cross into the subprocess).
def _find_root(marker: str = "project_root.txt") -> Path:
    try:
        start = Path(__file__).resolve().parent
    except NameError:
        start = Path.cwd().resolve()
    for p in (start, *start.parents):
        if (p / marker).is_file():
            return p
    raise RuntimeError(f"{marker!r} not found above {start}")


ROOT_DIR = Path(os.getenv("AIRFLOW_MGMT_ROOT") or _find_root()).resolve()
REPO_ROOT = ROOT_DIR.parent if (ROOT_DIR.parent / "ftp_handler").is_dir() else ROOT_DIR
for path in (REPO_ROOT, ROOT_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))
# ────────────────────────────────────────────────────────────────────────────

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
# Pinned for venv cache stability (cache key hashes the requirements list).
OPENSEARCH_PY = "opensearch-py==2.6.0"


def _collect_and_index(
    config: dict,
    repo_root: str,
    bucket: str,
    opensearch_index: str,
    tuning: dict,
    failure_threshold: float,
) -> dict:
    """Runs INSIDE the venv subprocess. All imports are local on purpose.

    ``config`` carries the values resolved by the upstream ``load_config`` task
    (fleet specs + FTP creds), so this function never touches Variable/Connection
    APIs the subprocess can't reach. ``repo_root`` is the dir holding the
    first-party packages (ftp_handler / ops_store / minio_handler); we re-add it
    to sys.path here because the DAG-file bootstrap doesn't cross into the venv.
    """
    import logging
    import sys as _sys
    from pathlib import Path as _Path

    rr = str(_Path(repo_root))
    if rr not in _sys.path:
        _sys.path.insert(0, rr)

    # opensearch-py (pip into the venv) is pulled in lazily by OSDoc; minio and
    # the FTP/glue code come from system_site_packages + repo_root.
    from ftp_handler.direct_downloader import build_host_specs, collect_fleet
    from minio_handler import MinioObject
    from ops_store import OSDoc

    log = logging.getLogger("airflow.task")

    def parse_records(host: str, remote_path: str, data: bytes) -> list[dict]:
        """YOUR processing seam — turn raw bytes into OpenSearch docs.

        NOTE: this lives inside the venv callable (not at module scope) because
        the subprocess can't see DAG-module functions. Replace this stub: branch
        on remote_path / file type, decode, build docs with a deterministic
        ``_id`` for idempotent re-runs. ``minio_key`` is stamped by collect_fleet.
        Return [] to index nothing for a file.
        """
        raise NotImplementedError("implement parse_records for your file formats")

    specs = build_host_specs(config["fleet"])

    # minio-py and opensearch-py clients are thread-safe (pooled), so one of each
    # is shared across the concurrent on_file callbacks. Config is read from the
    # worker's OPENSEARCH_* / MINIO_* env (inherited by this subprocess).
    storage = MinioObject(bucket=bucket)
    doc = OSDoc()

    def archive(host: str, remote_path: str, data: bytes) -> str:
        key = f"{host}/{remote_path.lstrip('/')}"
        storage.put(key, data)
        return key

    def index_docs(docs: list[dict]) -> None:
        doc.bulk_index(docs, index=opensearch_index)

    report = collect_fleet(
        specs,
        user=config["user"],
        password=config["password"],
        archive=archive,
        parse=parse_records,
        index=index_docs,
        port=config["port"],
        **tuning,
    )

    # Per-host/file failures are expected (equipment goes offline); log them but
    # only fail the task on a systemic problem.
    for failure in report.failures:
        log.warning("FTP collect failure: %s", failure)

    summary = {
        "hosts": len(specs),
        "ok": report.ok,
        "ng": report.ng,
        "failure_ratio": round(report.failure_ratio, 3),
    }
    log.info("eqp_ftp_collector summary: %s", summary)

    if report.ok == 0 or report.failure_ratio > failure_threshold:
        raise RuntimeError(
            f"eqp_ftp_collector systemic failure: {summary} "
            f"(threshold={failure_threshold})"
        )
    return summary


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
    def load_config() -> dict:
        """Resolve Variable + Connection in a normal task (the venv can't), and
        hand plain JSON-serializable values to the collect step via XCom."""
        from airflow.hooks.base import BaseHook
        from airflow.sdk import Variable

        conn = BaseHook.get_connection(FTP_CONN_ID)
        return {
            "fleet": Variable.get(FLEET_VARIABLE, deserialize_json=True),
            "user": conn.login,
            "password": conn.password,
            "port": conn.port or 21,
        }

    PythonVirtualenvOperator(
        task_id="collect_and_index",
        python_callable=_collect_and_index,
        op_kwargs={
            "config": load_config(),
            "repo_root": str(REPO_ROOT),
            "bucket": MINIO_BUCKET,
            "opensearch_index": OPENSEARCH_INDEX,
            "tuning": {
                "max_concurrency": MAX_CONCURRENCY,
                "connect_timeout": CONNECT_TIMEOUT,
                "host_timeout": HOST_TIMEOUT,
            },
            "failure_threshold": FAILURE_THRESHOLD,
        },
        # Only opensearch-py is missing on the workers; minio + airflow come from
        # system_site_packages. Pinned so repeated runs reuse the cached venv.
        requirements=[OPENSEARCH_PY],
        index_urls=NEXUS_PYPI_INDEX_URLS,
        system_site_packages=True,
        venv_cache_path="/opt/airflow/venv_cache",
    )
