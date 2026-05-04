"""Cross-OS safe FTP download + MinIO upload reference.

Runnable standalone (local dev). The same shape drops into an Airflow @task
unchanged — replace the `__main__` block with a `@task` wrapper.

Key idea: scratch storage lives in `tempfile.TemporaryDirectory`, never in
an OS-branched constant. Cleanup is guaranteed on every exit path
(success, exception, SIGTERM, KeyboardInterrupt).
"""

import asyncio
import sys
import tempfile
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


def collect_logs(ips: list[str]) -> dict:
    # ignore_cleanup_errors avoids a Windows PermissionError if a handle is
    # still open at __exit__ — the OS reclaims the dir later anyway.
    with tempfile.TemporaryDirectory(
        prefix="recipe_logs_", ignore_cleanup_errors=True
    ) as tmp:
        cwd_folder = Path(tmp)
        targets = build_targets(cwd_folder, ips)
        result = asyncio.run(
            ftp_download_async(
                targets, user=FTP_USER, password=FTP_PASSWORD, port=FTP_PORT
            )
        )
        return upload_results(result, cwd_folder, MINIO_BUCKET)


if __name__ == "__main__":
    sample_ips = ["10.0.0.1", "10.0.0.2"]
    summary = collect_logs(sample_ips)
    print(f"uploaded={summary['ok']} failed={summary['ng']}")
    for failure in summary["failed"]:
        print(f"  FAIL {failure}")
