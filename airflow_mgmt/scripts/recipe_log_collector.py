"""Recipe log collector — production version of ftp_download_sample.

Differences from the sample:
  - ftp_download_async is implemented (stdlib ftplib + asyncio.to_thread,
    no extra packages required on the worker).
  - upload_results writes date-partitioned MinIO keys derived from the
    YYYYMMDD_HHMMSS_* filename prefix. A single malformed name lands
    under unknown_date/ instead of breaking the batch.
  - collect_logs takes user / password / port as kwargs so the DAG can
    pull them from an Airflow Connection rather than baking them into
    source.

REMOTE_LOG_PATH is still a placeholder — replace with the real path on
the FTP servers before deploying. If the real workflow involves a
directory of timestamped files (rather than one fixed path), build_targets
needs to expand into one target per file per IP; ask before that change.

Deployment note: this Airflow 3.x setup isolates each task's filesystem
(verified via diagnostics_check_executor — different hostnames, marker
files don't cross task boundaries). Cleanup MUST stay in-process inside
collect_logs(); a separate end-of-DAG cleanup task can't see the files.
For "skip already done" behavior across retries, check MinIO state — not
local disk — since the local FS is fresh on every task instance.
"""

import asyncio
import logging
import shutil
import sys
import uuid
from datetime import datetime
from ftplib import FTP
from pathlib import Path
from typing import TypedDict

log = logging.getLogger(__name__)


# ── sys.path bootstrap ──────────────────────────────────────────────────────
# Same marker-file walk as the sample. The bootstrap MUST run before the
# minio_handler import below, since that import depends on airflow_mgmt/
# being on sys.path.
def _find_root(marker: str = "project_root.txt") -> Path:
    try:
        start = Path(__file__).resolve().parent
    except NameError:  # REPL / python -c / exec()
        start = Path.cwd().resolve()
    for p in (start, *start.parents):
        if (p / marker).is_file():
            return p
    raise RuntimeError(f"{marker!r} not found above {start}")


ROOT_DIR = _find_root()
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))
# ────────────────────────────────────────────────────────────────────────────


from minio_handler import MinioObject  # noqa: E402
from utils.scratch import scratch_root  # noqa: E402

SCRATCH_ROOT = scratch_root(ROOT_DIR)


REMOTE_LOG_PATH = "/HITACHI/SYSFILE/LOG_RECIPE_EXE.log"
MINIO_BUCKET = "eqp-logs"


class DownloadResult(TypedDict):
    files: dict


def _partition_from_name(name: str) -> str:
    """Return 'YYYY/MM/DD' from a '20260503_123050_*' filename.

    Falls back to 'unknown_date' so a single weirdly-named file does not
    fail the whole batch — the malformed files stay discoverable by
    listing the unknown_date/ prefix later.
    """
    stem = name.split("_", 1)[0]
    try:
        d = datetime.strptime(stem, "%Y%m%d")
    except ValueError:
        return "unknown_date"
    return d.strftime("%Y/%m/%d")


def build_targets(cwd_folder: Path, ips: list[str]) -> list[tuple[str, str, Path]]:
    return [
        (
            ip,
            REMOTE_LOG_PATH,
            cwd_folder / "LOG_RECIPE_LOG" / ip / Path(REMOTE_LOG_PATH).name,
        )
        for ip in ips
    ]


def _download_one_blocking(
    ip: str,
    remote_path: str,
    local_path: Path,
    user: str,
    password: str,
    port: int,
) -> dict:
    # Runs in a worker thread (one per IP) via asyncio.to_thread below.
    # retrbinary streams the body in chunks, so memory stays flat even
    # for multi-GB log files.
    local_path.parent.mkdir(parents=True, exist_ok=True)
    with FTP() as ftp:
        ftp.connect(host=ip, port=port, timeout=30)
        ftp.login(user=user, passwd=password)
        with local_path.open("wb") as fh:
            ftp.retrbinary(f"RETR {remote_path}", fh.write)
    return {"path": str(local_path), "ip": ip, "remote": remote_path}


async def ftp_download_async(
    targets: list[tuple[str, str, Path]],
    user: str,
    password: str,
    port: int = 21,
) -> DownloadResult:
    # return_exceptions=True so one unreachable host does not abort the
    # other downloads — partial success is the normal case in a fan-out.
    coros = [
        asyncio.to_thread(_download_one_blocking, ip, remote, local, user, password, port)
        for (ip, remote, local) in targets
    ]
    results = await asyncio.gather(*coros, return_exceptions=True)

    success: list[dict] = []
    failed: list[dict] = []
    for (ip, _remote, _local), outcome in zip(targets, results):
        if isinstance(outcome, Exception):
            failed.append({"ip": ip, "error": f"{type(outcome).__name__}: {outcome}"})
        else:
            success.append(outcome)
    return {"files": {"success": success, "failed": failed}}


def upload_results(result: DownloadResult, cwd_folder: Path, bucket: str) -> dict:
    storage = MinioObject(bucket=bucket)

    uploaded: list[dict] = []
    upload_failed: list[dict] = []
    for item in result["files"]["success"]:
        local_path = Path(item["path"])
        # Date partition first so MinIO lifecycle rules and "today's data"
        # listings stay cheap. local_path.parent.name is the IP, set by
        # build_targets above.
        partition = _partition_from_name(local_path.name)
        ip = local_path.parent.name
        key = f"{partition}/{ip}/{local_path.name}"

        try:
            storage.upload(key=key, file_path=local_path)
            uploaded.append(
                {"ip": ip, "key": key, "size": local_path.stat().st_size}
            )
        except Exception as exc:
            log.exception("MinIO upload failed for %s -> %s", local_path, key)
            upload_failed.append(
                {"ip": ip, "key": key, "error": f"{type(exc).__name__}: {exc}"}
            )

    download_failed = result["files"].get("failed", [])
    summary = {
        "uploaded": uploaded,
        "download_failed": download_failed,
        "upload_failed": upload_failed,
        "ok": len(uploaded),
        "ng": len(download_failed) + len(upload_failed),
    }

    # Fail the task on ANY failure (download or upload). Airflow retries
    # the whole task; MinIO PUT is idempotent so re-uploading already-
    # uploaded files just replaces them with identical content. The cost
    # is one extra round of FTP downloads on retry — acceptable, since
    # checking MinIO existence per-file would add LAN round-trips on
    # every successful run too.
    if download_failed or upload_failed:
        raise RuntimeError(
            f"recipe_log_collector partial failure: "
            f"{len(uploaded)} uploaded, "
            f"{len(download_failed)} ftp-failed, "
            f"{len(upload_failed)} upload-failed. "
            f"summary={summary}"
        )

    return summary


def cleanup_folder(path: Path) -> None:
    # Refuse to delete anything outside SCRATCH_ROOT — guards against a
    # caller accidentally passing SCRATCH_ROOT itself or "/".
    resolved = path.resolve()
    rel = resolved.relative_to(SCRATCH_ROOT.resolve())
    if rel == Path("."):
        raise ValueError(f"refusing to remove SCRATCH_ROOT itself: {resolved}")
    shutil.rmtree(resolved, ignore_errors=True)


def collect_logs(
    ips: list[str],
    *,
    user: str,
    password: str,
    port: int = 21,
    bucket: str = MINIO_BUCKET,
) -> dict:
    # uuid4 isolates concurrent task instances (.expand() fan-out) so
    # they never share a directory.
    cwd_folder = SCRATCH_ROOT / "recipe_logs" / uuid.uuid4().hex
    cwd_folder.mkdir(parents=True, exist_ok=True)
    try:
        targets = build_targets(cwd_folder, ips)
        result = asyncio.run(
            ftp_download_async(targets, user=user, password=password, port=port)
        )
        return upload_results(result, cwd_folder, bucket)
    finally:
        cleanup_folder(cwd_folder)
