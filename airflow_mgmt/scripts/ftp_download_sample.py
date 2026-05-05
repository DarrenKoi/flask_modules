"""Cross-OS safe FTP download + MinIO upload reference.

Runnable standalone (local dev). The same shape drops into an Airflow @task
unchanged — replace the `__main__` block with a `@task` wrapper.

Two filesystem locations, two purposes:
  - ROOT_DIR     → where the code lives (sys.path target). On Airflow this
                   is the read-only git mount, so we never write here.
  - SCRATCH_ROOT → where we write runtime files (downloads, working data).
                   On Airflow this defaults under /tmp; on dev boxes it's a subdir of
                   ROOT_DIR so the files stay inspectable.

Conflating these two is the classic "PermissionError: cannot mkdir under
the dags folder" bug — the airflow user has read access to the git mount
but not write access, by ops design.

Cleanup runs in a `finally` block. Airflow does not auto-clean task
working files; without explicit cleanup, downloads accumulate on the worker.
"""

import asyncio
import os
import shutil
import sys
import tempfile
import uuid
from pathlib import Path
from typing import TypedDict


# ── sys.path bootstrap ──────────────────────────────────────────────────────
# Find airflow_mgmt/ on disk (the directory holding the project_root.txt
# marker file) and put it on sys.path so repo-local packages
# (minio_handler/, ...) become importable as top-level names.
# Marker-file lookup is rename-safe — the parent folder can be called anything
# (repo/, dags_repo/, ...), only the marker inside has to be there.
# AIRFLOW_MGMT_ROOT env var overrides auto-detect — set it on Airflow workers
# if the parent walk can't find the marker file.
ROOT_DIR = Path(os.getenv("AIRFLOW_MGMT_ROOT") or next(
    (str(p) for p in Path(__file__).resolve().parents if (p / "project_root.txt").is_file()),
    "",
)).resolve()
if not ROOT_DIR.is_dir():
    raise RuntimeError("Cannot find airflow_mgmt root. Set AIRFLOW_MGMT_ROOT.")
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))
# ────────────────────────────────────────────────────────────────────────────


def _scratch_root() -> Path:
    """Writable runtime dir. Never under ROOT_DIR on Airflow (read-only mount).

    Order: AIRFLOW_MGMT_SCRATCH_ROOT env wins; else /tmp/airflow_mgmt on
    Airflow workers; else ROOT_DIR/scratch for local dev (inspectable).
    """
    env = os.getenv("AIRFLOW_MGMT_SCRATCH_ROOT")
    if env:
        return Path(env).expanduser().resolve()
    in_airflow = (
        any(os.getenv(n) for n in ("AIRFLOW_HOME", "AIRFLOW_CTX_DAG_ID", "AIRFLOW__CORE__DAGS_FOLDER"))
        or any(s in Path.cwd().as_posix() for s in ("/opt/airflow", "/ops/airflow"))
    )
    if in_airflow:
        return Path(tempfile.gettempdir()).resolve() / "airflow_mgmt"
    return ROOT_DIR / "scratch"


SCRATCH_ROOT = _scratch_root()

# Repo-local import: airflow_mgmt/minio_handler/ is importable as a top-level
# package now that airflow_mgmt/ is on sys.path.
from minio_handler import MinioObject  # noqa: E402


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
