"""Cross-OS safe FTP download + MinIO upload reference.

Runnable standalone (local dev). The same shape drops into an Airflow @task
unchanged — replace the `__main__` block with a `@task` wrapper.

Key idea: scratch storage lives in a uniquely-named subdir of ROOT_DIR,
created at the start of the run and removed in a `finally` block. Files
stay inspectable during the run (useful for debugging) and are gone the
moment the function returns — success or failure.

Airflow does NOT auto-clean task working files. Without an explicit cleanup,
downloaded files would accumulate on the worker forever.
"""

import asyncio
import shutil
import sys
import uuid
from pathlib import Path
from platform import system
from typing import TypedDict


def _root_dir() -> Path:
    if system() == "Windows":
        return Path("F:/skewnono")
    return Path("/project/workSpace")


ROOT_DIR = _root_dir()
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

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
    # Refuse to delete anything outside ROOT_DIR — guards against a caller
    # accidentally passing ROOT_DIR itself or "/". relative_to() raises
    # ValueError if `path` is not under ROOT_DIR, and we also reject the
    # root itself (relative path == ".").
    resolved = path.resolve()
    rel = resolved.relative_to(ROOT_DIR.resolve())
    if rel == Path("."):
        raise ValueError(f"refusing to remove ROOT_DIR itself: {resolved}")
    shutil.rmtree(resolved, ignore_errors=True)


def collect_logs(ips: list[str]) -> dict:
    # uuid4 isolates concurrent task instances (e.g. .expand() fan-out) so
    # they never share a directory.
    cwd_folder = ROOT_DIR / "recipe_logs" / uuid.uuid4().hex
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
