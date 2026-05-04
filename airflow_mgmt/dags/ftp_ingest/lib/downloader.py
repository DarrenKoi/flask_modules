"""
Pure-Python FTP/FTPS downloader.

No airflow imports — testable in plain pytest, callable from anywhere.
The DAG layer resolves Airflow Connections and passes plain creds in.
"""

import ftplib
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path


def download_to_path(
    *,
    host: str,
    user: str,
    password: str,
    remote_path: str,
    local_path: Path,
    port: int = 21,
    use_tls: bool = True,
    timeout: int = 60,
    ftp_factory: Callable[..., ftplib.FTP] | None = None,
) -> dict:
    """Download one file via FTP/FTPS to a local path.

    Returns metadata: remote_path, local_path, size_bytes, downloaded_at.
    Raises whatever ftplib raises on connection/auth/transfer failure —
    the DAG layer wraps with retries.

    `ftp_factory` is dependency-injection for tests; production uses the
    default ftplib classes.
    """
    factory = ftp_factory or (ftplib.FTP_TLS if use_tls else ftplib.FTP)
    local_path.parent.mkdir(parents=True, exist_ok=True)

    with factory(timeout=timeout) as ftp:
        ftp.connect(host, port)
        ftp.login(user, password)
        if use_tls:
            ftp.prot_p()  # encrypt the data channel for FTPS
        with local_path.open("wb") as f:
            ftp.retrbinary(f"RETR {remote_path}", f.write)

    return {
        "remote_path": remote_path,
        "local_path": str(local_path),
        "size_bytes": local_path.stat().st_size,
        "downloaded_at": datetime.now(timezone.utc).isoformat(),
    }
