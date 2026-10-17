"""proxy.FtpClient against the real proxy routes, compared with core.FtpClient.

The proxy server (flask_proxy) runs under Flask's test client and does its FTP
through core.FtpClient, so patching ``ftp_handler.core.client.FTP`` puts one
fake server behind both transports. Every scenario is run direct and through
the proxy, and the two must agree: same values, same exception types.
"""

import inspect
import json
import subprocess
import sys
import time
import unittest
from datetime import datetime, timedelta, timezone
from ftplib import error_perm
from pathlib import Path
from typing import ClassVar
from unittest.mock import patch
from urllib.parse import urlsplit

from ftp_handler.core import client as core_client
from ftp_handler.core.client import (
    FileInfo,
    ListingTooLarge,
    TransferTimeout,
    TransferTooLarge,
)
from ftp_handler.proxy import flask_proxy, proxy_client
from ftp_handler.proxy.proxy_client import (
    FileUnavailable,
    ProxyError,
    ToolUnreachable,
    WriteNotSupported,
)

FTP_PATCH_TARGET = "ftp_handler.core.client.FTP"
_ROOT = Path(__file__).resolve().parents[1]


class FakeFTP:
    """One scripted server: home, LIST lines, RETR chunks, optional faults."""

    home = "/"
    files: ClassVar[dict] = {}
    list_lines: ClassVar[dict] = {}
    pause = 0.0
    login_error = None
    connect_error = None
    logins: ClassVar[list] = []

    def __init__(self, timeout=None, encoding="utf-8"):
        pass

    def connect(self, host, port, timeout=None):
        if FakeFTP.connect_error is not None:
            raise FakeFTP.connect_error

    def login(self, user, passwd):
        FakeFTP.logins.append((user, passwd))
        if FakeFTP.login_error is not None:
            raise FakeFTP.login_error

    def set_pasv(self, value):
        pass

    def pwd(self):
        return FakeFTP.home

    def retrbinary(self, cmd, callback):
        path = cmd.split(" ", 1)[1]
        if path not in FakeFTP.files:
            raise error_perm(f"550 {path}: No such file or directory")
        for chunk in FakeFTP.files[path]:
            if FakeFTP.pause:
                time.sleep(FakeFTP.pause)
            callback(chunk)

    def retrlines(self, cmd, callback):
        path = cmd.split(" ", 1)[1]
        if path not in FakeFTP.list_lines:
            raise error_perm(f"550 {path}: No such file or directory")
        for line in FakeFTP.list_lines[path]:
            if FakeFTP.pause:
                time.sleep(FakeFTP.pause)
            callback(line)

    def close(self):
        pass


class FakeResponse:
    """The slice of ``requests.Response`` the proxy client reads."""

    def __init__(self, status_code, data, chunk=4):
        self.status_code = status_code
        self._data = data
        self._chunk = chunk
        self.closed = False

    def json(self):
        return json.loads(self._data)  # ValueError on non-JSON, as requests does

    def iter_content(self, chunk_size=1):
        for i in range(0, len(self._data), self._chunk):
            yield self._data[i : i + self._chunk]

    def close(self):
        self.closed = True


def _bridge(flask_client, rewrite=None):
    """A stand-in for requests.post that routes to the Flask test client."""

    def post(url, json=None, headers=None, timeout=None, stream=False):
        body = rewrite(dict(json)) if rewrite else json
        resp = flask_client.post(urlsplit(url).path, json=body, headers=headers or {})
        return FakeResponse(resp.status_code, resp.get_data())

    return post


def _canned(status, data):
    def post(url, json=None, headers=None, timeout=None, stream=False):
        return FakeResponse(status, data)

    return post


class ProxyClientParityTests(unittest.TestCase):
    def setUp(self) -> None:
        FakeFTP.home = "/"
        FakeFTP.files = {}
        FakeFTP.list_lines = {}
        FakeFTP.pause = 0.0
        FakeFTP.login_error = None
        FakeFTP.connect_error = None
        FakeFTP.logins = []
        patcher = patch(FTP_PATCH_TARGET, FakeFTP)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.flask = flask_proxy.create_app().test_client()
        self._use(_bridge(self.flask))

    def _use(self, post):
        patcher = patch.object(proxy_client.requests, "post", post)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _direct(self):
        return core_client.FtpClient(host="h1", user="u", password="p")

    def _proxied(self):
        return proxy_client.FtpClient(host="h1", user="u", password="p")

    def _both(self, op):
        """Run ``op(ftp)`` on each transport and return both outcomes."""
        out = []
        for make in (self._direct, self._proxied):
            try:
                with make() as ftp:
                    out.append(("ok", op(ftp)))
            except Exception as exc:  # noqa: BLE001 - the type is the result
                out.append(("raised", exc))
        return out

    def assertSameRaise(self, op, exc_type):
        direct, proxied = self._both(op)
        self.assertEqual(direct[0], "raised", direct)
        self.assertEqual(proxied[0], "raised", proxied)
        self.assertIsInstance(direct[1], exc_type)
        self.assertIsInstance(proxied[1], exc_type)
        return proxied[1]

    # -- results ----------------------------------------------------------

    def test_list_details_matches_the_direct_client(self):
        FakeFTP.list_lines = {
            "/log": [
                "total 8",
                "drwxr-xr-x 2 ftp ftp 4096 Jan 05 2024 sub",
                "-rw-r--r-- 1 ftp ftp 12 Feb 03 2024 a.txt",
                "lrwxrwxrwx 1 ftp ftp 4 Feb 03 2024 up -> ..",
                "10-02-24  03:15PM  345 b.CSV",
            ]
        }
        direct, proxied = self._both(lambda ftp: ftp.list_details("/log"))
        self.assertEqual(direct, proxied)
        self.assertEqual(
            proxied[1][1],
            FileInfo(
                name="a.txt",
                path="/log/a.txt",
                is_dir=False,
                size=12,
                modified=datetime(2024, 2, 3, tzinfo=timezone(timedelta(hours=9))),
                raw="-rw-r--r-- 1 ftp ftp 12 Feb 03 2024 a.txt",
            ),
        )
        self.assertTrue(proxied[1][2].is_link)

    def test_home_matches_the_direct_client(self):
        FakeFTP.home = "/home/ftpuser"
        FakeFTP.list_lines = {"/home/ftpuser": []}
        self.assertEqual(
            self._both(lambda ftp: ftp.home()),
            [("ok", "/home/ftpuser"), ("ok", "/home/ftpuser")],
        )

    def test_download_returns_the_same_raw_bytes(self):
        FakeFTP.files = {"/log/a.bin": [b"\x00\xff", b"\r\n", b"tail"]}
        self.assertEqual(
            self._both(lambda ftp: ftp.download("/log/a.bin", max_bytes=8)),
            [("ok", b"\x00\xff\r\ntail"), ("ok", b"\x00\xff\r\ntail")],
        )

    # -- the same exceptions ---------------------------------------------

    def test_an_oversized_download_is_transfer_too_large_on_both(self):
        FakeFTP.files = {"/log/a.bin": [b"x" * 10] * 3}
        self.assertSameRaise(
            lambda ftp: ftp.download("/log/a.bin", max_bytes=25), TransferTooLarge
        )

    def test_a_long_listing_is_listing_too_large_on_both(self):
        FakeFTP.list_lines = {"/log": ["junk line"] * 5}
        self.assertSameRaise(
            lambda ftp: ftp.list_details("/log", max_entries=3), ListingTooLarge
        )
        self.assertSameRaise(
            lambda ftp: ftp.list_details("/log", max_raw_bytes=20), ListingTooLarge
        )

    def test_a_trickling_download_is_transfer_timeout_on_both(self):
        FakeFTP.files = {"/log/slow.bin": [b"x"] * 20}
        FakeFTP.pause = 0.02
        self.assertSameRaise(
            lambda ftp: ftp.download("/log/slow.bin", deadline=0.05), TransferTimeout
        )

    def test_a_550_is_error_perm_on_both(self):
        exc = self.assertSameRaise(lambda ftp: ftp.download("/nope"), error_perm)
        self.assertIsInstance(exc, FileUnavailable)
        self.assertTrue(str(exc).startswith("550"))
        self.assertSameRaise(lambda ftp: ftp.list_details("/nope"), error_perm)

    def test_a_refused_login_is_an_ftplib_error_but_never_a_missing_file(self):
        from ftplib import all_errors

        FakeFTP.login_error = error_perm("530 Login incorrect")
        direct, proxied = self._both(lambda ftp: ftp.list_details("/"))
        # Direct refuses at __enter__, the proxy on its first request; both are
        # in ftplib.all_errors, and the proxy's must not pass for a 550.
        self.assertIsInstance(direct[1], error_perm)
        self.assertIsInstance(proxied[1], ToolUnreachable)
        self.assertIsInstance(proxied[1], all_errors)
        self.assertNotIsInstance(proxied[1], error_perm)

    def test_an_unreachable_tool_is_tool_unreachable(self):
        FakeFTP.connect_error = ConnectionRefusedError("refused")
        direct, proxied = self._both(lambda ftp: ftp.home())
        self.assertIsInstance(direct[1], OSError)
        self.assertIsInstance(proxied[1], ToolUnreachable)

    def test_the_client_caps_bytes_even_if_the_proxy_sends_more(self):
        # An older or misconfigured proxy ignoring max_bytes still cannot make
        # the client buffer past its cap.
        FakeFTP.files = {"/log/a.bin": [b"x" * 40]}
        self._use(_bridge(self.flask, rewrite=lambda b: {**b, "max_bytes": None}))
        with self._proxied() as ftp, self.assertRaises(TransferTooLarge):
            ftp.download("/log/a.bin", max_bytes=10)

    def test_the_envelope_carries_the_clients_account(self):
        FakeFTP.list_lines = {"/": []}
        with self._proxied() as ftp:
            ftp.home()
        self.assertEqual(FakeFTP.logins, [("u", "p")])

    def test_a_request_without_a_host_is_value_error(self):
        resp = self.flask.post(flask_proxy.URL_FETCH, json={"remote_path": "/a"})
        self.assertEqual(resp.status_code, 422)
        self.assertEqual(resp.get_json()["code"], "bad_request")
        self._use(_canned(422, resp.get_data()))
        with self._proxied() as ftp, self.assertRaises(ValueError):
            ftp.download("/a")

    # -- the proxy's own failures are ProxyError ----------------------------

    def test_proxy_failures_are_proxy_error(self):
        cases = {
            "401": (401, b'{"error": "unauthorized"}'),
            "404 html": (404, b"<html>Not Found</html>"),
            "404 with a code": (404, b'{"code": "unavailable", "error": "550 x"}'),
            "500": (500, b"<html>Internal Server Error</html>"),
            "502 harakiri": (502, b"Bad Gateway"),
            "200 not json": (200, b"<html>login page</html>"),
            "200 wrong json": (200, b'{"files": []}'),
        }
        for name, (status, data) in cases.items():
            with self.subTest(name):
                self._use(_canned(status, data))
                with self._proxied() as ftp, self.assertRaises(ProxyError):
                    ftp.list_details("/log")

    def test_an_unreachable_proxy_is_proxy_error(self):
        def down(*args, **kwargs):
            raise proxy_client.requests.ConnectionError("proxy down")

        self._use(down)
        with self._proxied() as ftp, self.assertRaises(ProxyError):
            ftp.download("/a")

    def test_the_proxy_token_check_turns_into_proxy_error(self):
        with patch.dict("os.environ", {"FTP_PROXY_TOKEN": "secret"}):
            with self._proxied() as ftp, self.assertRaises(ProxyError):
                ftp.home()
            client = self._proxied()
            client.token = "secret"
            FakeFTP.list_lines = {"/": []}
            with client as ftp:
                self.assertEqual(ftp.home(), "/")


class ProxyClientSurfaceTests(unittest.TestCase):
    def test_read_methods_have_the_direct_clients_exact_signature(self):
        for name in ("__init__", "home", "list_details", "download"):
            with self.subTest(name):
                self.assertEqual(
                    inspect.signature(getattr(proxy_client.FtpClient, name)),
                    inspect.signature(getattr(core_client.FtpClient, name)),
                )

    def test_writes_raise_write_not_supported(self):
        with proxy_client.FtpClient(host="h1", user="u", password="p") as ftp:
            with self.assertRaises(WriteNotSupported):
                ftp.put("/a.txt", b"x")
            with self.assertRaises(WriteNotSupported):
                ftp.remove("/a.txt")

    def test_the_package_exports_the_proxy_client(self):
        from ftp_handler import proxy

        self.assertIs(proxy.FtpClient, proxy_client.FtpClient)
        self.assertIn("FtpClient", proxy.__all__)
        self.assertIn("FtpFleetDownloader", proxy.__all__)

    def test_importing_the_proxy_client_does_not_import_flask(self):
        code = (
            "import sys; from ftp_handler.proxy import FtpClient; "
            "sys.exit('flask' in sys.modules)"
        )
        done = subprocess.run([sys.executable, "-c", code], cwd=_ROOT, check=False)
        self.assertEqual(done.returncode, 0)


if __name__ == "__main__":
    unittest.main()
