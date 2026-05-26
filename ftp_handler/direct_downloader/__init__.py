"""Direct FTP fleet download — talk to the equipment servers with no proxy.

Use this when the running process can reach the FTP servers directly (an
Airflow worker, a firewall-free Flask host). The public ``FtpFleetDownloader``
surface here is identical to ``ftp_handler.proxy`` — swap the import line to
route through the HTTP proxy instead, nothing else changes::

    from ftp_handler.direct_downloader import FtpFleetDownloader   # direct
    from ftp_handler.proxy             import FtpFleetDownloader   # via proxy
"""

from .collect import build_host_specs, collect_fleet
from .fleet_downloader import (
    DownloadReport,
    FileResult,
    FtpFleetDownloader,
    HostFailure,
    HostListing,
    HostSpec,
    ListDir,
    ListingReport,
    OnFile,
    download_fleet,
    list_fleet,
    save_to_dir,
    specs_from_hosts,
)

__all__ = [
    "FtpFleetDownloader",
    "download_fleet",
    "list_fleet",
    "HostSpec",
    "ListDir",
    "DownloadReport",
    "FileResult",
    "HostFailure",
    "HostListing",
    "ListingReport",
    "OnFile",
    "save_to_dir",
    "specs_from_hosts",
    "build_host_specs",
    "collect_fleet",
]
