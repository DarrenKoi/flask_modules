"""Cross-OS safe FTP download + MinIO upload reference.

Runnable standalone (local dev). The same shape drops into an Airflow
PythonOperator unchanged — replace the `__main__` block with an operator
callable wrapper.

Two filesystem locations, two purposes:
  - ROOT_DIR     → where the code lives (sys.path target). On Airflow this
                   is the read-only git mount, so we never write here.
  - SCRATCH_ROOT → where we write runtime files (downloads, working data).
                   On Airflow it's /tmp/<root_dir.name>; on dev boxes it's
                   ROOT_DIR/scratch so files stay inspectable in the IDE.

Conflating these two is the classic "PermissionError: cannot mkdir under
the dags folder" bug — the airflow user has read access to the git mount
but not write access, by ops design.

Cleanup runs in a `finally` block. Airflow does not auto-clean task
working files; without explicit cleanup, downloads accumulate on the worker.

Deployment note: this Airflow 3.x setup isolates each task's filesystem
(verified via diagnostics_check_executor — different hostnames, marker
files don't cross task boundaries). Cleanup MUST stay in-process inside
collect_logs(); a separate end-of-DAG cleanup task can't see the files.
For "skip already done" behavior across retries, check MinIO state — not
local disk — since the local FS is fresh on every task instance.
"""

import asyncio
import shutil
import sys
import uuid
from pathlib import Path
from typing import TypedDict


# ── sys.path bootstrap ──────────────────────────────────────────────────────
# Walks parents of this file (or cwd when run from a REPL) for the marker
# file and puts that directory on sys.path so repo-local packages
# (minio_handler/, ...) import as top-level names. Marker-file lookup is
# rename-safe — the parent folder can be called anything.
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


# Repo-local imports: airflow_mgmt/ is on sys.path now.
from minio_handler import MinioObject  # noqa: E402
from utils.scratch import scratch_root  # noqa: E402

SCRATCH_ROOT = scratch_root(ROOT_DIR)


REMOTE_LOG_PATH = "/HITACHI/SYSFILE/LOG_RECIPE_EXE.log"
MINIO_BUCKET = "eqp-logs"

FTP_USER = "ftpuser"
FTP_PASSWORD = "ftppass"
FTP_PORT = 21


class DownloadResult(TypedDict):
    files: dict  # {"success": [{"path": str, ...}], "failed": [{"ip": str, "error": str}]}


def build_targets(cwd_folder: Path, ips: list[str]) -> list[tuple[str, str, Path]]:
    return [
        (
            ip,
            REMOTE_LOG_PATH,
            cwd_folder / "LOG_RECIPE_LOG" / ip / Path(REMOTE_LOG_PATH).name,
        )
        for ip in ips
    ]


async def ftp_download_async(
    targets: list[tuple[str, str, Path]],
    user: str,
    password: str,
    port: int = 21,
) -> DownloadResult:
    """Replace this stub with the real async downloader.

    Contract the rest of the script depends on:
      - Each target is (ip, remote_path, local_path); local_path is fully resolved.
      - Function creates parent dirs and writes to local_path exactly.
      - Returns {"files": {"success": [{"path": str, ...}], "failed": [...]}}.
    """
    raise NotImplementedError("plug in your async ftp downloader here")


def upload_results(result: DownloadResult, cwd_folder: Path, bucket: str) -> dict:
    storage = MinioObject(bucket=bucket)

    uploaded: list[dict] = []
    for item in result["files"]["success"]:
        local_path = Path(item["path"])
        # relative_to(cwd_folder) gives "LOG_RECIPE_LOG/<ip>/<file>" — same on
        # every OS, regardless of whether cwd_folder is /tmp/xxx or C:\...\Temp\xxx.
        # as_posix() forces forward slashes so MinIO keys never get backslashes
        # when this runs on Windows.
        key = local_path.relative_to(cwd_folder).as_posix()
        storage.upload(key=key, file_path=local_path)
        uploaded.append(
            {"ip": local_path.parent.name, "key": key, "size": local_path.stat().st_size}
        )

    failed = result["files"].get("failed", [])
    return {"uploaded": uploaded, "failed": failed, "ok": len(uploaded), "ng": len(failed)}


def cleanup_folder(path: Path) -> None:
    # Refuse to delete anything outside SCRATCH_ROOT — guards against a
    # caller accidentally passing SCRATCH_ROOT itself or "/". relative_to()
    # raises ValueError if `path` is not under SCRATCH_ROOT, and we also
    # reject the root itself (relative path == ".").
    resolved = path.resolve()
    rel = resolved.relative_to(SCRATCH_ROOT.resolve())
    if rel == Path("."):
        raise ValueError(f"refusing to remove SCRATCH_ROOT itself: {resolved}")
    shutil.rmtree(resolved, ignore_errors=True)


def collect_logs(ips: list[str]) -> dict:
    # uuid4 isolates concurrent task instances (e.g. .expand() fan-out) so
    # they never share a directory. SCRATCH_ROOT, not ROOT_DIR — the latter
    # is the git mount on Airflow workers and not writable.
    cwd_folder = SCRATCH_ROOT / "recipe_logs" / uuid.uuid4().hex
    cwd_folder.mkdir(parents=True, exist_ok=True)
    try:
        targets = build_targets(cwd_folder, ips)
        result = asyncio.run(
            ftp_download_async(
                targets, user=FTP_USER, password=FTP_PASSWORD, port=FTP_PORT
            )
        )
        return upload_results(result, cwd_folder, MINIO_BUCKET)
    finally:
        cleanup_folder(cwd_folder)


if __name__ == "__main__":
    sample_ips = ["10.0.0.1", "10.0.0.2"]
    summary = collect_logs(sample_ips)
    print(f"uploaded={summary['ok']} failed={summary['ng']}")
    for failure in summary["failed"]:
        print(f"  FAIL {failure}")
