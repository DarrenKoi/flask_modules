"""HTTP-proxy FTP downloader — the client half, drop-in for ftp_fleet_downloader.

For a PC that CANNOT reach the equipment FTP servers directly (firewalled) but
CAN reach the Flask proxy (``ftp_handler/ftp_flask_proxy.py``) on a firewall-free
host. Exposes the SAME public names as ftp_fleet_downloader, so a call site
swaps with one import line and nothing else changes:

    # direct FTP (no firewall in the way):
    from ftp_fleet_downloader import FtpFleetDownloader, HostSpec, ListDir
    # via the proxy (firewalled client):
    from ftp_flask_downloader import FtpFleetDownloader, HostSpec, ListDir

The dataclasses (HostSpec, ListDir, DownloadReport, FileResult, HostFailure)
are re-exported from ftp_fleet_downloader — they are the *same types*, so
report.grouped(), report.failure_ratio, and your on_file handler behave
identically under either transport.

Transport: specs are split into batches and POSTed to the proxy concurrently;
the proxy does the real FTP and returns base64'd bytes. ``on_file`` still runs
HERE on the client, so your parse/archive/index processing stays local.

Proxy location & auth come from constructor args or env, so the constructor
signature stays compatible with the direct downloader:
    FTP_PROXY_URL    e.g. https://proxy.host:8080   (default http://localhost:8080)
    FTP_PROXY_TOKEN  bearer token the proxy enforces (optional)

Run: pip install requests
"""

import base64
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

# Reuse the SAME dataclasses so reports are interchangeable with direct mode.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ftp_fleet_downloader import (  # noqa: E402
    DownloadReport,
    FileResult,
    HostFailure,
    HostSpec,
    ListDir,
    OnFile,
    save_to_dir,
)

__all__ = [
    "FtpFleetDownloader",
    "download_fleet",
    "HostSpec",
    "ListDir",
    "DownloadReport",
    "FileResult",
    "HostFailure",
    "save_to_dir",
]


def _spec_to_wire(spec: HostSpec) -> dict:
    return {
        "host": spec.host,
        "files": list(spec.files),
        "listings": [
            {"remote_dir": ld.remote_dir, "pattern": ld.pattern}
            for ld in spec.listings
        ],
    }


class FtpFleetDownloader:
    """Same surface as ftp_fleet_downloader.FtpFleetDownloader, over HTTP.

    Extra args beyond the direct downloader (all defaulted, so direct-mode call
    sites keep working):
        proxy_url      proxy base URL (default env FTP_PROXY_URL or localhost)
        token          bearer token (default env FTP_PROXY_TOKEN)
        request_batch  hosts per HTTP request (bounds proxy-side transient RAM:
                       Mode-A collect + base64 + jsonify ~= 3x a batch's raw
                       bytes). Default 5; sized for ~10MB files on the shared
                       8GiB/reload-on-rss=1500 proxy host. See ADR 0001.
        client_workers concurrent in-flight requests to the proxy. Default 4 to
                       match the proxy's processes=4 — one batch per worker, no
                       stacking in a single worker's address space.
        http_timeout   per-request read timeout; defaults generously to the
                       proxy's worst-case batch time
    """

    def __init__(
        self,
        *,
        user: str,
        password: str,
        port: int = 21,
        max_concurrency: int = 48,
        connect_timeout: float = 8.0,
        host_timeout: float = 45.0,
        passive: bool = True,
        proxy_url: str | None = None,
        token: str | None = None,
        request_batch: int = 5,
        client_workers: int = 4,
        http_timeout: float | None = None,
    ) -> None:
        self.user = user
        self.password = password
        self.port = port
        self.max_concurrency = max_concurrency
        self.connect_timeout = connect_timeout
        self.host_timeout = host_timeout
        self.passive = passive
        self.proxy_url = (
            proxy_url or os.getenv("FTP_PROXY_URL", "http://localhost:8080")
        ).rstrip("/")
        self.token = token or os.getenv("FTP_PROXY_TOKEN")
        self.request_batch = request_batch
        self.client_workers = client_workers
        # Worst case: the proxy works one batch of hosts roughly serially in the
        # tail, so allow host_timeout per host in the batch plus slack.
        self.http_timeout = http_timeout or (host_timeout * request_batch + 30.0)

    def download(
        self,
        specs: list[HostSpec],
        *,
        on_file: OnFile | None = None,
    ) -> DownloadReport:
        """Synchronous, same signature as the direct downloader.

        Splits specs into batches, POSTs them to the proxy concurrently, and
        merges the results into one DownloadReport. A whole-batch transport
        failure marks every host in that batch failed — per-host isolation one
        level up.
        """
        if not specs:
            return DownloadReport(files=[], failures=[])

        batches = [
            specs[i : i + self.request_batch]
            for i in range(0, len(specs), self.request_batch)
        ]
        files: list[FileResult] = []
        failures: list[HostFailure] = []

        workers = max(1, min(len(batches), self.client_workers))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for batch_files, batch_failures in pool.map(
                lambda b: self._post_batch(b, on_file), batches
            ):
                files.extend(batch_files)
                failures.extend(batch_failures)
        return DownloadReport(files=files, failures=failures)

    def _post_batch(
        self,
        batch: list[HostSpec],
        on_file: OnFile | None,
    ) -> tuple[list[FileResult], list[HostFailure]]:
        payload = {
            "user": self.user,
            "password": self.password,
            "port": self.port,
            "max_concurrency": self.max_concurrency,
            "connect_timeout": self.connect_timeout,
            "host_timeout": self.host_timeout,
            "passive": self.passive,
            "specs": [_spec_to_wire(s) for s in batch],
        }
        headers = {}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"

        try:
            # Path carries the _sknn_v3 suffix to avoid collisions with routes
            # already mounted on the host Flask app. Must match the blueprint
            # in ftp_flask_proxy.py.
            resp = requests.post(
                f"{self.proxy_url}/download_sknn_v3",
                json=payload,
                headers=headers,
                timeout=self.http_timeout,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:  # noqa: BLE001 - whole-batch transport failure
            return [], [
                HostFailure(
                    host=s.host,
                    error=f"proxy request failed: {type(exc).__name__}: {exc}",
                )
                for s in batch
            ]

        files: list[FileResult] = []
        failures: list[HostFailure] = [
            HostFailure(
                host=item["host"],
                error=item["error"],
                remote_path=item.get("remote_path"),
            )
            for item in data.get("failures", [])
        ]
        for item in data.get("files", []):
            host = item["host"]
            remote_path = item["remote_path"]
            raw = base64.b64decode(item["data_b64"])
            if on_file is not None:
                # Same contract as direct mode: callback consumes the bytes,
                # the report keeps none; a callback raise fails that file only.
                try:
                    on_file(host, remote_path, raw)
                    files.append(FileResult(host=host, remote_path=remote_path, data=b""))
                except Exception as exc:  # noqa: BLE001
                    failures.append(
                        HostFailure(
                            host=host,
                            error=f"{type(exc).__name__}: {exc}",
                            remote_path=remote_path,
                        )
                    )
            else:
                files.append(FileResult(host=host, remote_path=remote_path, data=raw))
        return files, failures


def download_fleet(
    specs: list[HostSpec],
    *,
    user: str,
    password: str,
    on_file: OnFile | None = None,
    **kwargs: object,
) -> DownloadReport:
    """One-call wrapper, mirroring ftp_fleet_downloader.download_fleet."""
    downloader = FtpFleetDownloader(user=user, password=password, **kwargs)  # type: ignore[arg-type]
    return downloader.download(specs, on_file=on_file)
