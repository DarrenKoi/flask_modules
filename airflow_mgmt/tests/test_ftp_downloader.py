"""
Unit tests for ftp_ingest/lib/downloader.py.

The downloader is pure Python — these tests don't need Airflow installed.
ftplib is mocked via the `ftp_factory` injection point.
"""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from ftp_ingest.lib.downloader import download_to_path


def _make_fake_ftp(payload: bytes) -> tuple[MagicMock, MagicMock]:
    """Build a fake FTP context manager + factory that delivers `payload`."""
    fake_ftp = MagicMock()
    fake_ftp.__enter__.return_value = fake_ftp
    fake_ftp.__exit__.return_value = None

    def fake_retr(_cmd: str, callback) -> None:
        callback(payload)

    fake_ftp.retrbinary.side_effect = fake_retr
    factory = MagicMock(return_value=fake_ftp)
    return fake_ftp, factory


def test_download_writes_file_and_returns_metadata(tmp_path: Path) -> None:
    fake_ftp, factory = _make_fake_ftp(b"hello world")
    local = tmp_path / "out.csv"

    result = download_to_path(
        host="ftp.example.com",
        user="u",
        password="p",
        remote_path="/orders.csv",
        local_path=local,
        ftp_factory=factory,
    )

    assert local.read_bytes() == b"hello world"
    assert result["remote_path"] == "/orders.csv"
    assert result["local_path"] == str(local)
    assert result["size_bytes"] == 11
    assert "downloaded_at" in result


def test_download_calls_connect_login_retr_in_order(tmp_path: Path) -> None:
    fake_ftp, factory = _make_fake_ftp(b"x")

    download_to_path(
        host="ftp.example.com",
        user="alice",
        password="secret",
        remote_path="/foo.bin",
        local_path=tmp_path / "foo.bin",
        port=2121,
        ftp_factory=factory,
    )

    fake_ftp.connect.assert_called_once_with("ftp.example.com", 2121)
    fake_ftp.login.assert_called_once_with("alice", "secret")
    fake_ftp.retrbinary.assert_called_once()
    cmd_arg = fake_ftp.retrbinary.call_args[0][0]
    assert cmd_arg == "RETR /foo.bin"


def test_download_creates_parent_directories(tmp_path: Path) -> None:
    _, factory = _make_fake_ftp(b"data")
    nested = tmp_path / "deep" / "nested" / "dir" / "out.csv"

    download_to_path(
        host="h", user="u", password="p",
        remote_path="/r", local_path=nested,
        ftp_factory=factory,
    )

    assert nested.exists()
    assert nested.parent.is_dir()


def test_download_propagates_ftplib_errors(tmp_path: Path) -> None:
    fake_ftp, factory = _make_fake_ftp(b"")
    fake_ftp.login.side_effect = RuntimeError("530 auth failed")

    with pytest.raises(RuntimeError, match="auth failed"):
        download_to_path(
            host="h", user="u", password="bad",
            remote_path="/r", local_path=tmp_path / "x",
            ftp_factory=factory,
        )


def test_use_tls_calls_prot_p(tmp_path: Path) -> None:
    fake_ftp, factory = _make_fake_ftp(b"x")

    download_to_path(
        host="h", user="u", password="p",
        remote_path="/r", local_path=tmp_path / "x",
        use_tls=True,
        ftp_factory=factory,
    )
    fake_ftp.prot_p.assert_called_once()


def test_no_tls_skips_prot_p(tmp_path: Path) -> None:
    fake_ftp, factory = _make_fake_ftp(b"x")

    download_to_path(
        host="h", user="u", password="p",
        remote_path="/r", local_path=tmp_path / "x",
        use_tls=False,
        ftp_factory=factory,
    )
    fake_ftp.prot_p.assert_not_called()
