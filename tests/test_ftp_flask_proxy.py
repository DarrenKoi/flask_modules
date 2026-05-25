"""End-to-end tests for the FTP proxy pair.

Wires the real client (ftp_flask_downloader) to the real proxy
(ftp_flask_proxy) through Flask's test client, faking only the FTP layer. This
exercises both directions of the wire protocol and proves the client is a
drop-in for the direct downloader.

The proxy pair imports its sibling by BARE name (it is meant to be copied out
and run standalone on a client PC), so this test puts ``ftp_handler/`` on
sys.path and imports the modules the same way — that keeps the dataclasses a
single shared set, which the ``... is core....`` identity test depends on.
"""

import inspect
import os
import socket
import sys
import unittest
from ftplib import error_perm
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

FTP_HANDLER = Path(__file__).resolve().parent.parent / "ftp_handler"
if str(FTP_HANDLER) not in sys.path:
    sys.path.insert(0, str(FTP_HANDLER))

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


def _seam_params(func):
    # (name, kind) per parameter, excluding self; annotations and defaults are
    # ignored so a matching call shape passes regardless of how each adapter
    # spells its type hints.
    return [
        (p.name, p.kind)
        for p in inspect.signature(func).parameters.values()
        if p.name != "self"
    ]


class FtpProxyPairTests(unittest.TestCase):
    def setUp(self) -> None:
        FakeFTP.scripts = {}
        os.environ.pop("FTP_PROXY_TOKEN", None)

    def tearDown(self) -> None:
        FakeFTP.scripts = {}
        os.environ.pop("FTP_PROXY_TOKEN", None)

    def _download(self, specs, *, on_file=None, fail_hosts=None, token=None, **kw):
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

    def _list_dirs(self, specs, *, fail_hosts=None, token=None, **kw):
        app = proxy_mod.create_app()
        fclient = app.test_client()
        with patch.object(core, "FTP", FakeFTP), patch.object(
            client_mod.requests, "post", _bridge(fclient, fail_hosts)
        ):
            dl = client_mod.FtpFleetDownloader(
                user="u", password="p", token=token, client_workers=1, **kw
            )
            return dl.list_dirs(specs)

    def test_round_trip_returns_same_bytes(self):
        FakeFTP.scripts = {"h1": {"files": {"/log": b"hello"}}}
        report = self._download([HostSpec("h1", files=["/log"])])
        self.assertEqual(report.grouped(), {"h1": {"/log": b"hello"}})
        self.assertEqual(report.ng, 0)

    def test_listing_through_proxy(self):
        FakeFTP.scripts = {
            "h1": {
                "listing": {"/MEAS": ["a.dat", "b.txt"]},
                "files": {"/MEAS/a.dat": b"A"},
            }
        }
        report = self._download([HostSpec("h1", listings=[ListDir("/MEAS", "*.dat")])])
        self.assertEqual(report.grouped(), {"h1": {"/MEAS/a.dat": b"A"}})

    def test_on_file_runs_on_client(self):
        FakeFTP.scripts = {"h1": {"files": {"/log": b"hello"}}}
        seen = []
        report = self._download(
            [HostSpec("h1", files=["/log"])],
            on_file=lambda h, p, d: seen.append((h, p, d)),
        )
        self.assertEqual(seen, [("h1", "/log", b"hello")])  # bytes arrived locally
        self.assertTrue(all(f.data == b"" for f in report.files))  # not retained

    def test_ftp_failure_propagates_from_proxy(self):
        FakeFTP.scripts = {"h1": {"connect_error": socket.timeout("x")}}
        report = self._download([HostSpec("h1", files=["/log"])])
        self.assertEqual(report.ng, 1)
        self.assertEqual(report.failures[0].host, "h1")

    def test_batch_transport_failure_isolated(self):
        # request_batch=1 → one host per request; h2's request "fails" in
        # transport but h1 still succeeds.
        FakeFTP.scripts = {
            "h1": {"files": {"/log": b"ok"}},
            "h2": {"files": {"/log": b"x"}},
        }
        report = self._download(
            [HostSpec("h1", files=["/log"]), HostSpec("h2", files=["/log"])],
            fail_hosts={"h2"},
            request_batch=1,
        )
        self.assertEqual(report.grouped(), {"h1": {"/log": b"ok"}})
        self.assertEqual(report.ng, 1)
        self.assertEqual(report.failures[0].host, "h2")
        self.assertIn("proxy request failed", report.failures[0].error)

    def test_auth_enforced(self):
        os.environ["FTP_PROXY_TOKEN"] = "secret"
        FakeFTP.scripts = {"h1": {"files": {"/log": b"hello"}}}

        # Wrong/missing token → proxy 401 → client records batch failure.
        bad = self._download([HostSpec("h1", files=["/log"])], token="wrong")
        self.assertEqual(bad.ng, 1)
        self.assertEqual(bad.ok, 0)

        # Correct token → success.
        good = self._download([HostSpec("h1", files=["/log"])], token="secret")
        self.assertEqual(good.grouped(), {"h1": {"/log": b"hello"}})

    def test_report_types_are_shared_with_direct_downloader(self):
        # Interchangeability guarantee: same classes, not look-alikes.
        self.assertIs(client_mod.DownloadReport, core.DownloadReport)
        self.assertIs(client_mod.HostSpec, core.HostSpec)
        self.assertIs(client_mod.FileResult, core.FileResult)

    def test_empty_specs_short_circuits(self):
        report = self._download([])
        self.assertEqual(report.ok, 0)
        self.assertEqual(report.ng, 0)

    def test_adapter_satisfies_fleet_transport(self):
        # The interchange seam: both the direct and proxy downloaders must remain
        # swappable behind one import line. A method dropped from one side, or a
        # parameter that drifts on either seam method, fails HERE not at a call site.
        for adapter in (core.FtpFleetDownloader, client_mod.FtpFleetDownloader):
            with self.subTest(adapter=adapter.__module__):
                self.assertTrue(issubclass(adapter, core.FleetTransport))
                for name in ("download", "list_dirs"):
                    self.assertEqual(
                        _seam_params(getattr(adapter, name)),
                        _seam_params(getattr(core.FleetTransport, name)),
                    )

    def test_list_dirs_through_proxy_returns_discovered_paths(self):
        FakeFTP.scripts = {"h1": {"listing": {"/MEAS": ["a.dat", "b.txt"]}}}
        report = self._list_dirs(
            [HostSpec("h1", listings=[ListDir("/MEAS", "*.dat")])]
        )
        self.assertEqual(report.grouped(), {"h1": ["/MEAS/a.dat"]})
        self.assertEqual(report.total_paths, 1)
        self.assertEqual(report.ng, 0)

    def test_list_dirs_then_download_closes_the_loop(self):
        # The full look-before-you-download workflow, end to end over the proxy:
        # discover paths, feed them straight back into download via to_specs().
        FakeFTP.scripts = {
            "h1": {"listing": {"/MEAS": ["a.dat"]}, "files": {"/MEAS/a.dat": b"A"}}
        }
        listing = self._list_dirs(
            [HostSpec("h1", listings=[ListDir("/MEAS", "*.dat")])]
        )
        report = self._download(listing.to_specs())
        self.assertEqual(report.grouped(), {"h1": {"/MEAS/a.dat": b"A"}})

    def test_list_dirs_batch_transport_failure_isolated(self):
        FakeFTP.scripts = {
            "h1": {"listing": {"/MEAS": ["a.dat"]}},
            "h2": {"listing": {"/MEAS": ["b.dat"]}},
        }
        report = self._list_dirs(
            [
                HostSpec("h1", listings=[ListDir("/MEAS")]),
                HostSpec("h2", listings=[ListDir("/MEAS")]),
            ],
            fail_hosts=["h2"],
            request_batch=1,
        )
        self.assertEqual(report.grouped(), {"h1": ["/MEAS/a.dat"]})
        self.assertEqual(report.ng, 1)
        self.assertEqual(report.failures[0].host, "h2")

    def test_list_dirs_empty_specs_short_circuits(self):
        report = self._list_dirs([])
        self.assertEqual(report.ok, 0)
        self.assertEqual(report.ng, 0)

    def test_list_fleet_wrapper_matches_direct(self):
        # The proxy client's list_fleet mirrors ftp_fleet_downloader.list_fleet.
        FakeFTP.scripts = {"h1": {"listing": {"/MEAS": ["a.dat"]}}}
        app = proxy_mod.create_app()
        fclient = app.test_client()
        with patch.object(core, "FTP", FakeFTP), patch.object(
            client_mod.requests, "post", _bridge(fclient)
        ):
            report = client_mod.list_fleet(
                [HostSpec("h1", listings=[ListDir("/MEAS")])],
                user="u",
                password="p",
                client_workers=1,
            )
        self.assertEqual(report.grouped(), {"h1": ["/MEAS/a.dat"]})


if __name__ == "__main__":
    unittest.main()
