"""Worked examples for ftp_handler — single-server and fleet-scale FTP.

Not tests and not imported by anything; a copy-paste reference you run against
real servers. Each function is one self-contained use case. Fill in the
connection constants below (or pass your own) and call the example you want from
the ``__main__`` block at the bottom.

Two scales, picked by how many hosts you touch:
  - FtpClient        — one server, one connection, ad-hoc ops.
  - FtpFleetDownloader — hundreds of servers concurrently, with a failure report.
"""

from ftp_handler.ftp_client import FtpClient
from ftp_handler.ftp_fleet_downloader import (
    FtpFleetDownloader,
    HostSpec,
    ListDir,
    download_fleet,
    list_fleet,
    save_to_dir,
    specs_from_hosts,
)
from ftp_handler.eqp_ftp_collect import build_host_specs, collect_fleet

# ── connection constants — replace with your environment ────────────────────
HOST = "10.0.0.1"
USER = "ftpuser"
PASSWORD = "ftppass"

# A small fleet for the multi-host examples. In production this list comes from
# an Airflow Variable, not a literal — see example_fleet_specs_from_config.
FLEET_HOSTS = ["10.0.0.1", "10.0.0.2", "10.0.0.3"]


# ════════════════════════════════════════════════════════════════════════════
# Single server — FtpClient
# ════════════════════════════════════════════════════════════════════════════
def example_list_names() -> None:
    """NLST: just the names in a directory (no type, no size, no time).

    Lowest common denominator — works on every server. Use it when you only need
    paths and will fetch them anyway. ``pattern`` is an fnmatch glob on basenames.
    """
    with FtpClient(host=HOST, user=USER, password=PASSWORD) as ftp:
        names = ftp.list_dir("/MEAS", pattern="*.dat")
        for path in names:
            print(path)


def example_split_dirs_and_files() -> None:
    """MLSD: subfolders and files separated, when the server supports it.

    Cleanest way to tell folders from files. Raises ftplib.error_perm on an old
    daemon that lacks MLSD — fall back to list_details (LIST) there.
    """
    with FtpClient(host=HOST, user=USER, password=PASSWORD) as ftp:
        entries = ftp.list_entries("/MEAS", pattern="*.dat")
        print("subfolders:", entries.dirs)   # every subfolder
        print("files:", entries.files)        # only *.dat


def example_list_with_sizes_and_times() -> None:
    """LIST: typed entries with size and a timezone-aware modified time.

    The broadly-compatible fallback — parses the server's ls -l / MS-DOS text.
    Times are interpreted as KST by default; pass tz= to override. Useful for
    "only pull files modified since X" logic.
    """
    with FtpClient(host=HOST, user=USER, password=PASSWORD) as ftp:
        for info in ftp.list_details("/MEAS", pattern="*.dat"):
            kind = "dir " if info.is_dir else "file"
            print(f"{kind} {info.modified}  {info.size}  {info.path}")


def example_download_one_file() -> None:
    """RETR a single file's bytes into memory."""
    with FtpClient(host=HOST, user=USER, password=PASSWORD) as ftp:
        data = ftp.download("/HITACHI/SYSFILE/LOG_RECIPE_EXE.log")
        print(f"got {len(data)} bytes")


def example_upload_one_file() -> None:
    """STOR bytes to a remote path (overwrites if it exists)."""
    payload = b"col1,col2\n1,2\n"
    with FtpClient(host=HOST, user=USER, password=PASSWORD) as ftp:
        ftp.upload("/INBOX/report.csv", payload)


def example_remove_one_file() -> None:
    """DELE a remote path."""
    with FtpClient(host=HOST, user=USER, password=PASSWORD) as ftp:
        ftp.remove("/MEAS/stale.dat")


def example_many_ops_one_connection() -> None:
    """All four operations over one reused connection — the point of the
    context manager. Discover, fetch, archive a copy back, then clean up."""
    with FtpClient(host=HOST, user=USER, password=PASSWORD) as ftp:
        for path in ftp.list_dir("/MEAS", pattern="*.dat"):
            data = ftp.download(path)
            ftp.upload(f"/PROCESSED/{path.rsplit('/', 1)[-1]}", data)
            ftp.remove(path)


# ════════════════════════════════════════════════════════════════════════════
# Fleet — FtpFleetDownloader
# ════════════════════════════════════════════════════════════════════════════
def example_download_known_paths() -> None:
    """Pull fixed, known paths from every host concurrently.

    ``files`` are RETR'd directly with no listing — for append-only logs whose
    paths you already know. One connection per host, opened once and reused.
    """
    specs = [
        HostSpec(host, files=["/HITACHI/SYSFILE/LOG_RECIPE_EXE.log"])
        for host in FLEET_HOSTS
    ]
    dl = FtpFleetDownloader(user=USER, password=PASSWORD)
    report = dl.download(specs)

    print(f"ok={report.ok} ng={report.ng} failure_ratio={report.failure_ratio:.2f}")
    for f in report.files:
        print(f.host, f.remote_path, len(f.data))
    for x in report.failures:
        print("FAILED", x.host, x.remote_path, x.error)


def example_listing_then_download() -> None:
    """The "look before you download" pass for a large fleet.

    Step 1 lists each host's measurement dir concurrently (no fetching) so you
    can see the volume; step 2 feeds the discovered paths straight back into
    download via to_specs(). The decision in between is where you'd apply a
    threshold, a date filter, a cap, etc.

    specs_from_hosts wraps a plain IP list into specs when every host shares the
    same directories — the usual case for a uniform fleet.
    """
    discover = specs_from_hosts(FLEET_HOSTS, listings=[ListDir("/MEAS", "*.dat")])
    dl = FtpFleetDownloader(user=USER, password=PASSWORD)

    listing = dl.list_dirs(discover)
    print(f"discovered {listing.total_paths} files across {listing.ok} hosts")
    for host, paths in listing.grouped().items():
        print(host, len(paths))

    # ... decide what's worth pulling here ...
    report = dl.download(listing.to_specs())
    print(f"downloaded ok={report.ok} ng={report.ng}")


def example_streaming_to_disk() -> None:
    """Stream a large fleet to disk with bounded RAM.

    Passing on_file means each file is handed off the moment it lands and then
    dropped, so peak memory stays at concurrency x file size instead of the sum
    of the whole fleet. save_to_dir writes to dest/<host>/<remote path>.
    """
    specs = [HostSpec(host, listings=[ListDir("/MEAS", "*.dat")]) for host in FLEET_HOSTS]
    dl = FtpFleetDownloader(user=USER, password=PASSWORD)
    report = dl.download(specs, on_file=save_to_dir("/data/eqp_downloads"))
    print(f"wrote {report.ok} files, {report.ng} failures")


def example_one_call_helpers() -> None:
    """For callers that just want a function, not an object."""
    specs = [HostSpec(host, listings=[ListDir("/MEAS", "*.dat")]) for host in FLEET_HOSTS]

    listing = list_fleet(specs, user=USER, password=PASSWORD, max_concurrency=16)
    report = download_fleet(listing.to_specs(), user=USER, password=PASSWORD)
    print(report.ok, report.ng)


def example_tuning_for_large_fleet() -> None:
    """Constructor knobs for a ~300-host run.

    max_concurrency caps simultaneous connections (and, in memory mode, peak RAM
    ~= concurrency x file size). connect_timeout abandons a dead/black-holed host
    fast; host_timeout backstops a host that connects then stalls mid-transfer.
    passive=False is the escape hatch when a worker on a different subnet than
    your laptop needs active mode.
    """
    specs = [HostSpec(host, listings=[ListDir("/MEAS", "*.dat")]) for host in FLEET_HOSTS]
    dl = FtpFleetDownloader(
        user=USER,
        password=PASSWORD,
        max_concurrency=24,
        connect_timeout=8.0,
        host_timeout=60.0,
        passive=True,
    )
    report = dl.list_dirs(specs)
    print(f"discovered {report.total_paths} files")


# ════════════════════════════════════════════════════════════════════════════
# Fleet glue — archive → parse → index in one pass
# ════════════════════════════════════════════════════════════════════════════
def example_fleet_specs_from_config() -> None:
    """Build specs from deserialized JSON (e.g. an Airflow Variable)."""
    fleet = [
        {
            "host": "10.0.0.1",
            "files": ["/HITACHI/SYSFILE/LOG_RECIPE_EXE.log"],
            "listings": [{"remote_dir": "/MEAS", "pattern": "*.dat"}],
        },
        {"host": "10.0.0.2", "listings": [{"remote_dir": "/MEAS", "pattern": "*.dat"}]},
    ]
    specs = build_host_specs(fleet)
    print([s.host for s in specs])


def example_collect_archive_parse_index() -> None:
    """Download the fleet and, per file, archive → parse → index in memory.

    The three steps are callables you supply, so this stays free of minio /
    opensearch imports. archive runs first (never index a record whose raw
    source wasn't stored); a raise from any step fails just that file. Replace
    the fakes below with MinioObject.put_*, your parser, and OSDoc.bulk_index.
    """
    def archive(host: str, remote_path: str, data: bytes) -> str:
        # e.g. MinioObject(...).put_bytes(key, data); return key
        return f"raw/{host}{remote_path}"

    def parse(host: str, remote_path: str, data: bytes) -> list[dict]:
        # YOUR processing: bytes -> list of OpenSearch docs
        return [{"host": host, "path": remote_path, "raw_len": len(data)}]

    def index(docs: list[dict]) -> None:
        # e.g. OSDoc(...).bulk_index("meas_index", docs)
        print("would index", docs)

    specs = [HostSpec(host, listings=[ListDir("/MEAS", "*.dat")]) for host in FLEET_HOSTS]
    report = collect_fleet(
        specs,
        user=USER,
        password=PASSWORD,
        archive=archive,
        parse=parse,
        index=index,
    )
    print(f"processed ok={report.ok} ng={report.ng}")


if __name__ == "__main__":
    # Uncomment the example you want to run against your servers.
    # example_list_names()
    # example_split_dirs_and_files()
    # example_list_with_sizes_and_times()
    # example_listing_then_download()
    # example_collect_archive_parse_index()
    pass
