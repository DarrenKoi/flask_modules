"""Unit tests for the single-server FtpClient.

No live FTP server: ftp_handler.ftp_client.FTP is patched with FakeFTP, a
minimal stand-in driven by class-level state. Asserts the four operations issue
the right ftplib calls and that the connection lifecycle (connect/login/pasv on
enter, close on exit) runs.
"""

from datetime import datetime
from ftplib import error_perm
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

from ftp_handler.ftp_client import (
    DirEntries,
    FileInfo,
    FtpClient,
    _normalize_listing,
    _parse_list_line,
)

KST = ZoneInfo("Asia/Seoul")
# Fixed "now" so year inference for the year-less Unix format is deterministic.
NOW = datetime(2026, 5, 22, 12, 0, tzinfo=KST)


class FakeFTP:
    """Stand-in for ftplib.FTP. Per-test state lives on class attributes;
    records the calls FtpClient makes so they can be asserted."""

    listing: dict = {}
    mlsd_listing: dict = {}
    list_lines: dict = {}
    files: dict = {}
    stored: dict = {}
    deleted: list = []
    events: list = []

    def __init__(self, timeout=None):
        self.timeout = timeout

    def connect(self, host, port, timeout=None):
        FakeFTP.events.append(("connect", host, port))

    def login(self, user, passwd):
        FakeFTP.events.append(("login", user, passwd))

    def set_pasv(self, value):
        FakeFTP.events.append(("set_pasv", value))

    def nlst(self, remote_dir):
        if remote_dir not in FakeFTP.listing:
            raise error_perm(f"550 No such directory: {remote_dir}")
        return FakeFTP.listing[remote_dir]

    def mlsd(self, remote_dir):
        if remote_dir not in FakeFTP.mlsd_listing:
            raise error_perm(f"550 No such directory: {remote_dir}")
        return iter(FakeFTP.mlsd_listing[remote_dir])

    def retrlines(self, cmd, callback):
        # cmd is "LIST <remote_dir>"
        remote_dir = cmd.split(" ", 1)[1]
        for line in FakeFTP.list_lines.get(remote_dir, []):
            callback(line)

    def retrbinary(self, cmd, callback):
        path = cmd.split(" ", 1)[1]
        if path not in FakeFTP.files:
            raise error_perm(f"550 No such file: {path}")
        callback(FakeFTP.files[path])

    def storbinary(self, cmd, fp):
        path = cmd.split(" ", 1)[1]
        FakeFTP.stored[path] = fp.read()

    def delete(self, path):
        FakeFTP.deleted.append(path)

    def close(self):
        FakeFTP.events.append(("close",))


@pytest.fixture(autouse=True)
def _reset():
    FakeFTP.listing = {}
    FakeFTP.mlsd_listing = {}
    FakeFTP.list_lines = {}
    FakeFTP.files = {}
    FakeFTP.stored = {}
    FakeFTP.deleted = []
    FakeFTP.events = []
    yield


def _client(**kwargs) -> FtpClient:
    return FtpClient(host="h1", user="u", password="p", **kwargs)


def test_context_manager_connects_logs_in_and_closes():
    with patch("ftp_handler.ftp_client.FTP", FakeFTP):
        with _client(passive=False):
            pass
    assert FakeFTP.events == [
        ("connect", "h1", 21),
        ("login", "u", "p"),
        ("set_pasv", False),
        ("close",),
    ]


def test_list_dir_filters_by_pattern():
    FakeFTP.listing = {"/MEAS": ["a.dat", "b.txt", "c.dat"]}
    with patch("ftp_handler.ftp_client.FTP", FakeFTP):
        with _client() as ftp:
            assert ftp.list_dir("/MEAS", pattern="*.dat") == ["/MEAS/a.dat", "/MEAS/c.dat"]


def test_list_dir_no_pattern_returns_all_normalized():
    FakeFTP.listing = {"/MEAS": ["/MEAS/x.dat", "y.dat"]}
    with patch("ftp_handler.ftp_client.FTP", FakeFTP):
        with _client() as ftp:
            assert ftp.list_dir("/MEAS") == ["/MEAS/x.dat", "/MEAS/y.dat"]


def test_list_entries_splits_dirs_and_files():
    FakeFTP.mlsd_listing = {
        "/MEAS": [
            (".", {"type": "cdir"}),
            ("..", {"type": "pdir"}),
            ("2026", {"type": "dir"}),
            ("archive", {"type": "dir"}),
            ("a.dat", {"type": "file"}),
            ("notes.txt", {"type": "file"}),
        ]
    }
    with patch("ftp_handler.ftp_client.FTP", FakeFTP):
        with _client() as ftp:
            entries = ftp.list_entries("/MEAS")
    assert entries == DirEntries(
        dirs=["/MEAS/2026", "/MEAS/archive"],
        files=["/MEAS/a.dat", "/MEAS/notes.txt"],
    )


def test_list_entries_pattern_filters_files_only():
    FakeFTP.mlsd_listing = {
        "/MEAS": [
            ("sub.dat", {"type": "dir"}),  # a dir literally named like a file
            ("a.dat", {"type": "file"}),
            ("b.txt", {"type": "file"}),
        ]
    }
    with patch("ftp_handler.ftp_client.FTP", FakeFTP):
        with _client() as ftp:
            entries = ftp.list_entries("/MEAS", pattern="*.dat")
    # Pattern applies only to files; the dir is returned regardless.
    assert entries.dirs == ["/MEAS/sub.dat"]
    assert entries.files == ["/MEAS/a.dat"]


# ── LIST line parsing (pure, no connection) ─────────────────────────────────
def test_parse_unix_file_recent_infers_current_year():
    # Year-less recent form -> current year (the file's month is before NOW).
    name, is_dir, size, modified = _parse_list_line(
        "-rw-r--r--   1 owner group   123456 May 21 10:30 a.dat", NOW
    )
    assert (name, is_dir, size) == ("a.dat", False, 123456)
    assert modified == datetime(2026, 5, 21, 10, 30, tzinfo=KST)


def test_parse_unix_file_recent_rolls_back_future_date():
    # A Dec date seen in May must be last year, not a future this-year date.
    _, _, _, modified = _parse_list_line(
        "-rw-r--r--   1 owner group   10 Dec 31 23:59 old.log", NOW
    )
    assert modified == datetime(2025, 12, 31, 23, 59, tzinfo=KST)


def test_parse_unix_file_old_form_has_year_no_time():
    _, _, _, modified = _parse_list_line(
        "-rw-r--r--   1 owner group   10 Feb  3  2023 ancient.dat", NOW
    )
    assert modified == datetime(2023, 2, 3, 0, 0, tzinfo=KST)


def test_parse_unix_dir_has_no_size():
    name, is_dir, size, _ = _parse_list_line(
        "drwxr-xr-x   2 owner group   4096 May 21 10:30 2026", NOW
    )
    assert (name, is_dir, size) == ("2026", True, None)


def test_parse_unix_numeric_owner_still_finds_size():
    # Some servers print numeric UID/GID; size is still the number before month.
    _, _, size, _ = _parse_list_line(
        "-rw-r--r-- 1 1000 1000 4242 May 21 10:30 f.dat", NOW
    )
    assert size == 4242


def test_parse_unix_symlink_drops_target():
    name, is_dir, _, _ = _parse_list_line(
        "lrwxrwxrwx 1 o g 7 May 21 10:30 cur -> /MEAS/2026", NOW
    )
    assert name == "cur"


def test_parse_dos_file_two_digit_year_pm():
    name, is_dir, size, modified = _parse_list_line(
        "05-21-26  10:30PM              123456 report.dat", NOW
    )
    assert (name, is_dir, size) == ("report.dat", False, 123456)
    assert modified == datetime(2026, 5, 21, 22, 30, tzinfo=KST)


def test_parse_dos_dir_and_midnight_am():
    name, is_dir, size, modified = _parse_list_line(
        "05-21-26  12:00AM       <DIR>          archive", NOW
    )
    assert (name, is_dir, size) == ("archive", True, None)
    assert modified == datetime(2026, 5, 21, 0, 0, tzinfo=KST)


def test_parse_dos_four_digit_year():
    _, _, _, modified = _parse_list_line(
        "12-31-2025  11:59PM            5678 y.dat", NOW
    )
    assert modified == datetime(2025, 12, 31, 23, 59, tzinfo=KST)


def test_parse_unrecognized_line_returns_none():
    assert _parse_list_line("total 8", NOW) is None
    assert _parse_list_line("", NOW) is None


def test_list_details_integration_filters_and_parses():
    FakeFTP.list_lines = {
        "/MEAS": [
            "total 12",
            "drwxr-xr-x   2 owner group   4096 May 21 10:30 sub",
            "-rw-r--r--   1 owner group    100 May 21 10:31 a.dat",
            "-rw-r--r--   1 owner group    200 May 21 10:32 b.txt",
        ]
    }
    with patch("ftp_handler.ftp_client.FTP", FakeFTP):
        with _client() as ftp:
            entries = ftp.list_details("/MEAS", pattern="*.dat", tz=KST)

    # header skipped; the .txt filtered out (pattern, file); the dir kept.
    by_name = {e.name: e for e in entries}
    assert set(by_name) == {"sub", "a.dat"}
    assert by_name["sub"].is_dir and by_name["sub"].path == "/MEAS/sub"
    assert by_name["a.dat"].size == 100
    assert by_name["a.dat"].raw.endswith("a.dat")
    assert all(isinstance(e, FileInfo) for e in entries)


def test_download_returns_bytes():
    FakeFTP.files = {"/log.txt": b"hello"}
    with patch("ftp_handler.ftp_client.FTP", FakeFTP):
        with _client() as ftp:
            assert ftp.download("/log.txt") == b"hello"


def test_upload_stores_bytes():
    with patch("ftp_handler.ftp_client.FTP", FakeFTP):
        with _client() as ftp:
            ftp.upload("/INBOX/report.csv", b"col1,col2\n")
    assert FakeFTP.stored == {"/INBOX/report.csv": b"col1,col2\n"}


def test_remove_deletes_path():
    with patch("ftp_handler.ftp_client.FTP", FakeFTP):
        with _client() as ftp:
            ftp.remove("/MEAS/stale.dat")
    assert FakeFTP.deleted == ["/MEAS/stale.dat"]


def test_server_error_propagates():
    # Single-server caller wants the real exception, not a swallowed report.
    with patch("ftp_handler.ftp_client.FTP", FakeFTP):
        with _client() as ftp:
            with pytest.raises(error_perm):
                ftp.download("/missing")


def test_normalize_listing_handles_bare_and_full_paths():
    assert _normalize_listing(["a.dat", "/MEAS/b.dat"], "/MEAS") == [
        "/MEAS/a.dat",
        "/MEAS/b.dat",
    ]
    assert _normalize_listing(["a.dat", "b.txt"], "/MEAS/", "*.dat") == ["/MEAS/a.dat"]
