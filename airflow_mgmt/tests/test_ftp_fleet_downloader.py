"""Unit tests for the FTP fleet downloader.

No live FTP server: ftp_handler.ftp_fleet_downloader.FTP is patched with FakeFTP,
whose behavior per host is driven by a class-level script the test sets up.
Asserts the behaviors the design hinges on: per-host error isolation, both
discovery modes, on_file streaming, and threshold math.
"""

import socket
from ftplib import error_perm
from unittest.mock import patch

import pytest

from ftp_handler.ftp_fleet_downloader import (
    DownloadReport,
    FtpFleetDownloader,
    HostSpec,
    ListDir,
    download_fleet,
    save_to_dir,
)
from ftp_handler.ftp_fleet_downloader import _safe_relative


class FakeFTP:
    """Stand-in for ftplib.FTP. Per-host behavior comes from FakeFTP.scripts:

        FakeFTP.scripts = {
            "host": {
                "connect_error": Exception | None,
                "login_error": Exception | None,
                "files": {remote_path: bytes | Exception},
                "listing": {remote_dir: list[str] | Exception},
            }
        }
    """

    scripts: dict = {}

    def __init__(self, timeout=None):
        self.timeout = timeout
        self.host = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def _script(self) -> dict:
        return FakeFTP.scripts.get(self.host, {})

    def connect(self, host, port, timeout=None):
        self.host = host
        err = FakeFTP.scripts.get(host, {}).get("connect_error")
        if err is not None:
            raise err

    def login(self, user, passwd):
        err = self._script().get("login_error")
        if err is not None:
            raise err

    def set_pasv(self, value):
        self.passive = value

    def nlst(self, remote_dir):
        listing = self._script().get("listing", {})
        if remote_dir not in listing:
            raise error_perm(f"550 No such directory: {remote_dir}")
        value = listing[remote_dir]
        if isinstance(value, Exception):
            raise value
        return value

    def retrbinary(self, cmd, callback):
        remote_path = cmd.split(" ", 1)[1]
        files = self._script().get("files", {})
        if remote_path not in files:
            raise error_perm(f"550 No such file: {remote_path}")
        value = files[remote_path]
        if isinstance(value, Exception):
            raise value
        callback(value)


@pytest.fixture(autouse=True)
def _reset_scripts():
    FakeFTP.scripts = {}
    yield
    FakeFTP.scripts = {}


def _run(specs, **kwargs) -> DownloadReport:
    with patch("ftp_handler.ftp_fleet_downloader.FTP", FakeFTP):
        dl = FtpFleetDownloader(user="u", password="p", **kwargs)
        return dl.download(specs)


def test_fixed_path_download_returns_bytes():
    FakeFTP.scripts = {"h1": {"files": {"/log.txt": b"hello"}}}
    report = _run([HostSpec("h1", files=["/log.txt"])])

    assert report.ok == 1
    assert report.ng == 0
    assert report.files[0].host == "h1"
    assert report.files[0].remote_path == "/log.txt"
    assert report.files[0].data == b"hello"


def test_per_host_error_isolation():
    # h1's connect blows up; h2 must still download. One dead host never
    # aborts the rest of the fleet.
    FakeFTP.scripts = {
        "h1": {"connect_error": socket.timeout("timed out")},
        "h2": {"files": {"/log.txt": b"ok"}},
    }
    report = _run(
        [HostSpec("h1", files=["/log.txt"]), HostSpec("h2", files=["/log.txt"])]
    )

    assert report.ok == 1
    assert report.ng == 1
    assert report.grouped() == {"h2": {"/log.txt": b"ok"}}
    failed = report.failures[0]
    assert failed.host == "h1"
    assert failed.remote_path is None  # failed before any file (connect)


def test_listing_pattern_filters():
    FakeFTP.scripts = {
        "h1": {
            "listing": {"/MEAS": ["a.dat", "b.txt", "c.dat"]},
            "files": {"/MEAS/a.dat": b"A", "/MEAS/c.dat": b"C"},
        }
    }
    report = _run([HostSpec("h1", listings=[ListDir("/MEAS", "*.dat")])])

    got = report.grouped()["h1"]
    assert set(got) == {"/MEAS/a.dat", "/MEAS/c.dat"}
    assert got["/MEAS/a.dat"] == b"A"


def test_listing_returns_full_paths_normalized():
    # Some FTP servers return full paths from NLST; RETR must still work.
    FakeFTP.scripts = {
        "h1": {
            "listing": {"/MEAS": ["/MEAS/x.dat"]},
            "files": {"/MEAS/x.dat": b"X"},
        }
    }
    report = _run([HostSpec("h1", listings=[ListDir("/MEAS", "*.dat")])])
    assert report.grouped() == {"h1": {"/MEAS/x.dat": b"X"}}


def test_fixed_and_listing_combine_on_one_connection():
    FakeFTP.scripts = {
        "h1": {
            "listing": {"/MEAS": ["m1.dat"]},
            "files": {"/log.txt": b"L", "/MEAS/m1.dat": b"M"},
        }
    }
    report = _run(
        [HostSpec("h1", files=["/log.txt"], listings=[ListDir("/MEAS", "*.dat")])]
    )
    assert report.grouped() == {"h1": {"/log.txt": b"L", "/MEAS/m1.dat": b"M"}}


def test_listing_failure_isolated_from_fixed_files():
    # The directory listing fails, but the fixed-path file on the same host
    # still downloads. The listing failure is recorded separately.
    FakeFTP.scripts = {
        "h1": {
            "listing": {},  # /MEAS not present -> nlst raises error_perm
            "files": {"/log.txt": b"L"},
        }
    }
    report = _run(
        [HostSpec("h1", files=["/log.txt"], listings=[ListDir("/MEAS", "*.dat")])]
    )
    assert report.grouped() == {"h1": {"/log.txt": b"L"}}
    assert report.ng == 1
    assert report.failures[0].remote_path == "/MEAS"


def test_missing_file_recorded_per_file():
    FakeFTP.scripts = {"h1": {"files": {"/exists": b"y"}}}
    report = _run([HostSpec("h1", files=["/exists", "/missing"])])

    assert report.ok == 1
    assert report.ng == 1
    assert report.failures[0].remote_path == "/missing"


def test_on_file_streaming_drops_bytes():
    FakeFTP.scripts = {"h1": {"files": {"/a": b"A", "/b": b"B"}}}
    seen: list[tuple[str, str, bytes]] = []

    def on_file(host, remote_path, data):
        seen.append((host, remote_path, data))

    with patch("ftp_handler.ftp_fleet_downloader.FTP", FakeFTP):
        dl = FtpFleetDownloader(user="u", password="p")
        report = dl.download([HostSpec("h1", files=["/a", "/b"])], on_file=on_file)

    # Callback saw the real bytes...
    assert {(h, p, d) for h, p, d in seen} == {
        ("h1", "/a", b"A"),
        ("h1", "/b", b"B"),
    }
    # ...but the report retained none (RAM stays bounded).
    assert report.ok == 2
    assert all(f.data == b"" for f in report.files)
    assert report.grouped() == {"h1": {"/a": b"", "/b": b""}}


def test_on_file_exception_marks_that_file_failed():
    FakeFTP.scripts = {"h1": {"files": {"/good": b"G", "/bad": b"B"}}}

    def on_file(host, remote_path, data):
        if remote_path == "/bad":
            raise RuntimeError("index write failed")

    with patch("ftp_handler.ftp_fleet_downloader.FTP", FakeFTP):
        dl = FtpFleetDownloader(user="u", password="p")
        report = dl.download([HostSpec("h1", files=["/good", "/bad"])], on_file=on_file)

    assert report.ok == 1
    assert report.ng == 1
    assert report.failures[0].remote_path == "/bad"
    assert "index write failed" in report.failures[0].error


def test_failure_ratio_math():
    FakeFTP.scripts = {
        "h1": {"files": {"/a": b"A"}},
        "h2": {"connect_error": socket.timeout("x")},
        "h3": {"connect_error": socket.timeout("x")},
    }
    report = _run(
        [
            HostSpec("h1", files=["/a"]),
            HostSpec("h2", files=["/a"]),
            HostSpec("h3", files=["/a"]),
        ]
    )
    assert report.ok == 1
    assert report.ng == 2
    assert report.failure_ratio == pytest.approx(2 / 3)


def test_empty_report_failure_ratio_is_zero():
    assert DownloadReport(files=[], failures=[]).failure_ratio == 0.0


def test_download_fleet_helper():
    FakeFTP.scripts = {"h1": {"files": {"/log": b"data"}}}
    with patch("ftp_handler.ftp_fleet_downloader.FTP", FakeFTP):
        report = download_fleet(
            [HostSpec("h1", files=["/log"])], user="u", password="p", max_concurrency=4
        )
    assert report.grouped() == {"h1": {"/log": b"data"}}


# ── glue helper: build_host_specs + collect_fleet ───────────────────────────
from ftp_handler.eqp_ftp_collect import build_host_specs, collect_fleet  # noqa: E402


def test_build_host_specs_maps_files_and_listings():
    fleet = [
        {
            "host": "10.0.0.1",
            "files": ["/log.txt"],
            "listings": [{"remote_dir": "/MEAS", "pattern": "*.dat"}],
        },
        {"host": "10.0.0.2"},  # no files / listings
    ]
    specs = build_host_specs(fleet)

    assert specs[0].host == "10.0.0.1"
    assert specs[0].files == ["/log.txt"]
    assert specs[0].listings == [ListDir("/MEAS", "*.dat")]
    assert specs[1].host == "10.0.0.2"
    assert specs[1].files == []
    assert specs[1].listings == []


def test_collect_fleet_archives_parses_and_indexes_in_order():
    FakeFTP.scripts = {"h1": {"files": {"/a": b"raw-a"}}}
    calls: list[str] = []
    indexed: list[dict] = []

    def archive(host, remote_path, data):
        calls.append("archive")
        return f"bucket/{host}{remote_path}"

    def parse(host, remote_path, data):
        calls.append("parse")
        return [{"host": host, "raw_len": len(data)}]

    def index(docs):
        calls.append("index")
        indexed.extend(docs)

    with patch("ftp_handler.ftp_fleet_downloader.FTP", FakeFTP):
        report = collect_fleet(
            [HostSpec("h1", files=["/a"])],
            user="u",
            password="p",
            archive=archive,
            parse=parse,
            index=index,
        )

    assert report.ok == 1
    # archive strictly before parse before index, per the agreed strictness.
    assert calls == ["archive", "parse", "index"]
    # minio_key stamped onto every doc.
    assert indexed == [{"host": "h1", "raw_len": 5, "minio_key": "bucket/h1/a"}]


# ── save_to_dir disk helper ─────────────────────────────────────────────────
from pathlib import Path  # noqa: E402


def test_safe_relative_posix_path():
    assert _safe_relative("/HITACHI/SYSFILE/LOG.log") == Path("HITACHI/SYSFILE/LOG.log")


def test_safe_relative_windows_ftp_backslashes():
    # A Windows-hosted FTP server may return backslash-separated paths.
    assert _safe_relative("\\MEAS\\sub\\x.dat") == Path("MEAS/sub/x.dat")


def test_safe_relative_strips_traversal_and_drive():
    # No escaping the dest dir; a Windows drive letter's colon is sanitized.
    assert _safe_relative("/../../etc/passwd") == Path("etc/passwd")
    assert _safe_relative("C:/data/x") == Path("C_/data/x")


def test_safe_relative_sanitizes_illegal_chars():
    # A Linux filename with chars illegal on Windows must not crash a write.
    assert _safe_relative("/m/a:b?c.dat") == Path("m/a_b_c.dat")


def test_safe_relative_empty_falls_back():
    assert _safe_relative("/") == Path("_unnamed")


def test_save_to_dir_writes_files(tmp_path):
    FakeFTP.scripts = {
        "10.0.0.1": {"files": {"/HITACHI/SYSFILE/LOG.log": b"L"}},
        "10.0.0.2": {"files": {"/MEAS/x.dat": b"X"}},
    }
    with patch("ftp_handler.ftp_fleet_downloader.FTP", FakeFTP):
        dl = FtpFleetDownloader(user="u", password="p")
        report = dl.download(
            [
                HostSpec("10.0.0.1", files=["/HITACHI/SYSFILE/LOG.log"]),
                HostSpec("10.0.0.2", files=["/MEAS/x.dat"]),
            ],
            on_file=save_to_dir(tmp_path),
        )

    assert report.ok == 2
    assert (tmp_path / "10.0.0.1" / "HITACHI" / "SYSFILE" / "LOG.log").read_bytes() == b"L"
    assert (tmp_path / "10.0.0.2" / "MEAS" / "x.dat").read_bytes() == b"X"
    # Streaming write — report retains no bytes.
    assert all(f.data == b"" for f in report.files)


def test_save_to_dir_then_chains(tmp_path):
    FakeFTP.scripts = {"h1": {"files": {"/a": b"A"}}}
    chained = []
    with patch("ftp_handler.ftp_fleet_downloader.FTP", FakeFTP):
        dl = FtpFleetDownloader(user="u", password="p")
        dl.download(
            [HostSpec("h1", files=["/a"])],
            on_file=save_to_dir(tmp_path, then=lambda h, p, d: chained.append((h, p, d))),
        )
    assert (tmp_path / "h1" / "a").read_bytes() == b"A"
    assert chained == [("h1", "/a", b"A")]


def test_collect_fleet_index_failure_marks_file_failed():
    # If OpenSearch indexing throws, that file is a failure (archive succeeded
    # but the unit isn't done) — and siblings are unaffected.
    FakeFTP.scripts = {"h1": {"files": {"/a": b"A", "/b": b"B"}}}

    def archive(host, remote_path, data):
        return "k"

    def parse(host, remote_path, data):
        return [{"p": remote_path}]

    def index(docs):
        if docs[0]["p"] == "/b":
            raise RuntimeError("opensearch down")

    with patch("ftp_handler.ftp_fleet_downloader.FTP", FakeFTP):
        report = collect_fleet(
            [HostSpec("h1", files=["/a", "/b"])],
            user="u",
            password="p",
            archive=archive,
            parse=parse,
            index=index,
        )

    assert report.ok == 1
    assert report.ng == 1
    assert report.failures[0].remote_path == "/b"
    assert "opensearch down" in report.failures[0].error
