"""End-to-end tests for the FTP proxy pair.

Wires the real client (ftp_flask_downloader) to the real proxy
(ftp_flask_proxy) through Flask's test client, faking only the FTP layer. This
exercises both directions of the wire protocol and proves the client is a
drop-in for the direct downloader.

Modules are imported by their top-level names (matching how the client imports
its sibling on a real client PC), so the dataclasses are a single shared set.
"""

import socket
import sys
from ftplib import error_perm
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

import pytest

PKG = Path(__file__).resolve().parent.parent / "ftp_handler"
sys.path.insert(0, str(PKG))

import ftp_fleet_downloader as core  # noqa: E402
import ftp_flask_downloader as client_mod  # noqa: E402
import ftp_flask_proxy as proxy_mod  # noqa: E402
from ftp_fleet_downloader import HostSpec, ListDir  # noqa: E402


class FakeFTP:
    scripts: dict = {}

    def __init__(self, timeout=None):
        self.host = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def _script(self):
        return FakeFTP.scripts.get(self.host, {})

    def connect(self, host, port, timeout=None):
        self.host = host
        err = FakeFTP.scripts.get(host, {}).get("connect_error")
        if err is not None:
            raise err

    def login(self, user, passwd):
        pass

    def set_pasv(self, value):
        pass

    def nlst(self, remote_dir):
        listing = self._script().get("listing", {})
        if remote_dir not in listing:
            raise error_perm(f"550 {remote_dir}")
        return listing[remote_dir]

    def retrbinary(self, cmd, callback):
        remote_path = cmd.split(" ", 1)[1]
        files = self._script().get("files", {})
        if remote_path not in files:
            raise error_perm(f"550 {remote_path}")
        callback(files[remote_path])


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    FakeFTP.scripts = {}
    monkeypatch.delenv("FTP_PROXY_TOKEN", raising=False)
    yield
    FakeFTP.scripts = {}


def _bridge(flask_client, fail_hosts=None):
    """A stand-in for requests.post that routes to the Flask test client.

    If a posted batch contains a host in fail_hosts, raise to simulate a
    transport failure for that batch.
    """
    fail_hosts = set(fail_hosts or [])

    def fake_post(url, json=None, headers=None, timeout=None):
        hosts = {s["host"] for s in json.get("specs", [])}
        if hosts & fail_hosts:
            raise client_mod.requests.ConnectionError("simulated proxy down")
        resp = flask_client.post(urlsplit(url).path, json=json, headers=headers or {})

        class R:
            status_code = resp.status_code

            def raise_for_status(self):
                if resp.status_code >= 400:
                    raise client_mod.requests.HTTPError(str(resp.status_code))

            def json(self):
                return resp.get_json()

        return R()

    return fake_post


def _download(specs, *, on_file=None, fail_hosts=None, token=None, **kw):
    app = proxy_mod.create_app()
    fclient = app.test_client()
    # client_workers=1 keeps the Flask test client single-threaded in tests.
    with patch.object(core, "FTP", FakeFTP), patch.object(
        client_mod.requests, "post", _bridge(fclient, fail_hosts)
    ):
        dl = client_mod.FtpFleetDownloader(
            user="u", password="p", token=token, client_workers=1, **kw
        )
        return dl.download(specs, on_file=on_file)


def test_round_trip_returns_same_bytes():
    FakeFTP.scripts = {"h1": {"files": {"/log": b"hello"}}}
    report = _download([HostSpec("h1", files=["/log"])])
    assert report.grouped() == {"h1": {"/log": b"hello"}}
    assert report.ng == 0


def test_listing_through_proxy():
    FakeFTP.scripts = {
        "h1": {"listing": {"/MEAS": ["a.dat", "b.txt"]}, "files": {"/MEAS/a.dat": b"A"}}
    }
    report = _download([HostSpec("h1", listings=[ListDir("/MEAS", "*.dat")])])
    assert report.grouped() == {"h1": {"/MEAS/a.dat": b"A"}}


def test_on_file_runs_on_client():
    FakeFTP.scripts = {"h1": {"files": {"/log": b"hello"}}}
    seen = []
    report = _download(
        [HostSpec("h1", files=["/log"])],
        on_file=lambda h, p, d: seen.append((h, p, d)),
    )
    assert seen == [("h1", "/log", b"hello")]  # bytes arrived locally
    assert all(f.data == b"" for f in report.files)  # not retained


def test_ftp_failure_propagates_from_proxy():
    FakeFTP.scripts = {"h1": {"connect_error": socket.timeout("x")}}
    report = _download([HostSpec("h1", files=["/log"])])
    assert report.ng == 1
    assert report.failures[0].host == "h1"


def test_batch_transport_failure_isolated():
    # request_batch=1 → one host per request; h2's request "fails" in transport
    # but h1 still succeeds.
    FakeFTP.scripts = {"h1": {"files": {"/log": b"ok"}}, "h2": {"files": {"/log": b"x"}}}
    report = _download(
        [HostSpec("h1", files=["/log"]), HostSpec("h2", files=["/log"])],
        fail_hosts={"h2"},
        request_batch=1,
    )
    assert report.grouped() == {"h1": {"/log": b"ok"}}
    assert report.ng == 1
    assert report.failures[0].host == "h2"
    assert "proxy request failed" in report.failures[0].error


def test_auth_enforced(monkeypatch):
    monkeypatch.setenv("FTP_PROXY_TOKEN", "secret")
    FakeFTP.scripts = {"h1": {"files": {"/log": b"hello"}}}

    # Wrong/missing token → proxy 401 → client records batch failure.
    bad = _download([HostSpec("h1", files=["/log"])], token="wrong")
    assert bad.ng == 1 and bad.ok == 0

    # Correct token → success.
    good = _download([HostSpec("h1", files=["/log"])], token="secret")
    assert good.grouped() == {"h1": {"/log": b"hello"}}


def test_report_types_are_shared_with_direct_downloader():
    # Interchangeability guarantee: same classes, not look-alikes.
    assert client_mod.DownloadReport is core.DownloadReport
    assert client_mod.HostSpec is core.HostSpec
    assert client_mod.FileResult is core.FileResult


def test_empty_specs_short_circuits():
    report = _download([])
    assert report.ok == 0 and report.ng == 0
