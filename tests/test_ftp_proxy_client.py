"""proxy.FtpClient against the real proxy routes, compared with core.FtpClient.

The proxy server (flask_proxy) runs under Flask's test client and does its FTP
through core.FtpClient, so patching ``ftp_handler.core.client.FTP`` puts one
fake server behind both transports. Every scenario is run direct and through
the proxy, and the two must agree: same values, same exception types.
"""

import copy
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
    TargetExists,
    TransferTimeout,
    TransferTooLarge,
    TransferUncertain,
)
from ftp_handler.proxy import flask_proxy, proxy_client
from ftp_handler.proxy.proxy_client import (
    FileUnavailable,
    ProxyError,
    ToolUnreachable,
)

FTP_PATCH_TARGET = "ftp_handler.core.client.FTP"
_ROOT = Path(__file__).resolve().parents[1]


def _fresh(exc: BaseException) -> BaseException:
    # A real server's error is a new object per reply; reusing one instance
    # across the direct and proxied runs would let put's temp_path on one
    # leak onto the other's result.
    return type(exc)(*exc.args)


class FakeFTP:
    """One scripted server: home, LIST lines, RETR chunks, a writable tree, faults."""

    home = "/"
    files: ClassVar[dict] = {}
    dirs: ClassVar[set] = set()
    rename_error = None
    delete_error = None
    list_lines: ClassVar[dict] = {}
    pause = 0.0
    login_error = None
    connect_error = None
    pwd_error = None
    list_error = None
    logins: ClassVar[list] = []
    listed: ClassVar[list] = []

    def __init__(self, timeout=None, encoding="utf-8"):
        self.encoding = encoding

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
        if FakeFTP.pwd_error is not None:
            raise FakeFTP.pwd_error
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
        FakeFTP.listed.append(path)
        if FakeFTP.list_error is not None:
            raise FakeFTP.list_error
        if path not in FakeFTP.list_lines:
            raise error_perm(f"550 {path}: No such file or directory")
        for line in FakeFTP.list_lines[path]:
            if FakeFTP.pause:
                time.sleep(FakeFTP.pause)
            callback(line)

    def storbinary(self, cmd, fp, callback=None):
        cmd.encode(self.encoding)  # ftplib sends the command in the server charset
        path = cmd.split(" ", 1)[1]
        FakeFTP.files[path] = [fp.read()]

    def voidcmd(self, cmd):
        pass

    def size(self, path):
        if path not in FakeFTP.files:
            raise error_perm(f"550 {path}: No such file or directory")
        return sum(len(c) for c in FakeFTP.files[path])

    def rename(self, src, dst):
        if FakeFTP.rename_error is not None:
            raise _fresh(FakeFTP.rename_error)
        FakeFTP.files[dst] = FakeFTP.files.pop(src)

    def delete(self, path):
        if FakeFTP.delete_error is not None:
            raise _fresh(FakeFTP.delete_error)
        if path not in FakeFTP.files:
            raise error_perm(f"550 {path}: No such file or directory")
        del FakeFTP.files[path]

    def mkd(self, path):
        if path in FakeFTP.dirs:
            raise error_perm(f"550 {path}: File exists")
        FakeFTP.dirs.add(path)

    def rmd(self, path):
        if path not in FakeFTP.dirs:
            raise error_perm(f"550 {path}: No such file or directory")
        FakeFTP.dirs.remove(path)

    def cwd(self, path):
        if path not in FakeFTP.dirs and path != FakeFTP.home:
            raise error_perm(f"550 {path}: No such file or directory")

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
        _bridge.last_timeout = timeout
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
        FakeFTP.dirs = set()
        FakeFTP.rename_error = None
        FakeFTP.delete_error = None
        FakeFTP.list_lines = {}
        FakeFTP.pause = 0.0
        FakeFTP.login_error = None
        FakeFTP.connect_error = None
        FakeFTP.pwd_error = None
        FakeFTP.list_error = None
        FakeFTP.logins = []
        FakeFTP.listed = []
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
        return [self._run(make, op) for make in (self._direct, self._proxied)]

    def _run(self, make, op):
        try:
            with make() as ftp:
                return ("ok", op(ftp))
        except Exception as exc:  # noqa: BLE001 - the type is the result
            return ("raised", exc)

    def _both_writes(self, op):
        """``_both`` for a write: each transport starts from the same tree.

        Returns ``(outcome, files, dirs)`` per transport, the tree as the
        write left it, so a parity check covers the server side too.
        """
        start = (copy.deepcopy(FakeFTP.files), set(FakeFTP.dirs))
        out = []
        for make in (self._direct, self._proxied):
            FakeFTP.files, FakeFTP.dirs = copy.deepcopy(start[0]), set(start[1])
            out.append((self._run(make, op), FakeFTP.files, FakeFTP.dirs))
        return out

    def assertSameWrite(self, op, exc_type=None):
        direct, proxied = self._both_writes(op)
        self.assertEqual(direct[1:], proxied[1:])  # same tree afterwards
        if exc_type is None:
            self.assertEqual(direct[0], ("ok", None))
            self.assertEqual(proxied[0], ("ok", None))
            return direct[1:]
        for outcome in (direct[0], proxied[0]):
            self.assertEqual(outcome[0], "raised", outcome)
            self.assertIsInstance(outcome[1], exc_type)
        return proxied[0][1]

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

    def test_a_request_without_a_host_is_proxy_error(self):
        # A malformed request is a client/proxy version mismatch, not a value
        # the caller passed: it must read as the proxy failing, never as a bare
        # ValueError a caller's handlers do not expect.
        resp = self.flask.post(flask_proxy.URL_FETCH, json={"remote_path": "/a"})
        self.assertEqual(resp.status_code, 422)
        self.assertEqual(resp.get_json()["code"], "bad_request")
        self._use(_canned(422, resp.get_data()))
        with self._proxied() as ftp, self.assertRaises(ProxyError):
            ftp.download("/a")

    # -- home() is a PWD, never a LIST --------------------------------------

    def test_home_does_not_list_the_home_folder(self):
        # No LIST lines scripted for the home dir: a LIST there would 550.
        FakeFTP.home = "/home/ftpuser"
        self.assertEqual(
            self._both(lambda ftp: ftp.home()),
            [("ok", "/home/ftpuser"), ("ok", "/home/ftpuser")],
        )
        self.assertEqual(FakeFTP.listed, [])

    def test_home_waits_a_finite_time_for_the_proxy(self):
        FakeFTP.home = "/"
        with self._proxied() as ftp:
            ftp.home()
        _, read = _bridge.last_timeout
        self.assertIsNotNone(read)
        self.assertLessEqual(read, 2 * ftp.timeout)

    def test_the_home_only_route_answers_home_and_no_entries(self):
        FakeFTP.home = "/home/ftpuser"
        resp = self.flask.post(
            flask_proxy.URL_LIST_DETAILS,
            json={"host": "h1", "remote_dir": None, "home_only": True},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json(), {"home": "/home/ftpuser", "entries": []})
        self.assertEqual(FakeFTP.listed, [])

    def test_an_older_caller_without_home_only_still_lists_home(self):
        FakeFTP.home = "/home/ftpuser"
        FakeFTP.list_lines = {"/home/ftpuser": ["-rw-r--r-- 1 ftp ftp 1 Feb 03 2024 a"]}
        resp = self.flask.post(
            flask_proxy.URL_LIST_DETAILS, json={"host": "h1", "remote_dir": None}
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertEqual(body["home"], "/home/ftpuser")
        self.assertEqual([e["path"] for e in body["entries"]], ["/home/ftpuser/a"])

    # -- encoding errors are UnicodeDecodeError on both ---------------------

    def test_an_undecodable_listing_is_unicode_decode_error_on_both(self):
        FakeFTP.list_error = UnicodeDecodeError("utf-8", b"\xb0", 0, 1, "bad byte")
        self.assertSameRaise(lambda ftp: ftp.list_details("/log"), UnicodeDecodeError)

    def test_an_undecodable_pwd_is_unicode_decode_error_on_both(self):
        FakeFTP.pwd_error = UnicodeDecodeError("utf-8", b"\xb0", 0, 1, "bad byte")
        self.assertSameRaise(lambda ftp: ftp.home(), UnicodeDecodeError)

    def test_an_undecodable_banner_is_unicode_decode_error_on_both(self):
        FakeFTP.connect_error = UnicodeDecodeError("utf-8", b"\xb0", 0, 1, "bad byte")
        self.assertSameRaise(lambda ftp: ftp.list_details("/log"), UnicodeDecodeError)
        resp = self.flask.post(
            flask_proxy.URL_LIST_DETAILS, json={"host": "h1", "remote_dir": "/log"}
        )
        self.assertEqual(resp.status_code, 422)
        self.assertEqual(resp.get_json()["code"], "encoding")

    # -- writes match the direct client -------------------------------------

    def test_upload_overwrites_on_both(self):
        FakeFTP.files = {"/in/a.bin": [b"old"]}
        files, _ = self.assertSameWrite(lambda ftp: ftp.upload("/in/a.bin", b"\x00\xffnew"))
        self.assertEqual(files, {"/in/a.bin": [b"\x00\xffnew"]})

    def test_put_writes_a_new_file_and_leaves_no_temp_on_both(self):
        files, _ = self.assertSameWrite(lambda ftp: ftp.put("/in/a.bin", b"\r\nraw"))
        self.assertEqual(files, {"/in/a.bin": [b"\r\nraw"]})

    def test_put_onto_an_existing_file_is_target_exists_on_both(self):
        FakeFTP.files = {"/in/a.bin": [b"keep"]}
        self.assertSameWrite(lambda ftp: ftp.put("/in/a.bin", b"new"), TargetExists)
        self.assertEqual(FakeFTP.files, {"/in/a.bin": [b"keep"]})

    def test_a_lost_rename_reply_is_transfer_uncertain_with_its_temp_on_both(self):
        # The temp name is random per call, so each side is checked against
        # the temp it reported rather than the two trees against each other.
        FakeFTP.rename_error = EOFError()
        for (kind, exc), files, _ in self._both_writes(lambda ftp: ftp.put("/in/a.bin", b"x")):
            self.assertEqual(kind, "raised")
            self.assertIsInstance(exc, TransferUncertain)
            self.assertRegex(exc.temp_path, r"^/in/a\.bin\.[0-9a-f]{12}\.part$")
            self.assertEqual(files, {exc.temp_path: [b"x"]})

    def test_a_refused_rename_names_the_temp_it_could_not_delete_on_both(self):
        FakeFTP.rename_error = error_perm("553 rename refused")
        FakeFTP.delete_error = error_perm("550 delete refused")
        for (kind, exc), files, _ in self._both_writes(lambda ftp: ftp.put("/in/a.bin", b"x")):
            self.assertEqual(kind, "raised")
            self.assertIsInstance(exc, error_perm)
            self.assertTrue(str(exc).startswith("553"))
            self.assertEqual(files, {exc.temp_path: [b"x"]})
            self.assertIn(f"temp file left on server: {exc.temp_path}", exc.__notes__)

    def test_an_undecodable_rename_reply_keeps_the_temp_on_both(self):
        FakeFTP.rename_error = UnicodeDecodeError("utf-8", b"\xb0", 0, 1, "bad byte")
        FakeFTP.delete_error = error_perm("550 delete refused")
        for (kind, exc), files, _ in self._both_writes(lambda ftp: ftp.put("/in/a.bin", b"x")):
            self.assertEqual(kind, "raised")
            self.assertIsInstance(exc, UnicodeDecodeError)
            self.assertEqual(files, {exc.temp_path: [b"x"]})

    def test_an_unencodable_path_is_unicode_encode_error_on_both(self):
        def run(cls):
            return self._run(
                lambda: cls(host="h1", user="u", password="p", encoding="latin-1"),
                lambda ftp: ftp.upload("/in/측정.bin", b"x"),
            )

        for kind, exc in (run(core_client.FtpClient), run(proxy_client.FtpClient)):
            self.assertEqual(kind, "raised")
            self.assertIsInstance(exc, UnicodeEncodeError)

    def test_small_writes_wait_a_finite_time_for_the_proxy(self):
        FakeFTP.files = {"/in/a.bin": [b"x"]}
        FakeFTP.dirs = {"/in/old"}
        for op in (lambda f: f.remove("/in/a.bin"), lambda f: f.mkdir("/in/new"),
                   lambda f: f.rmdir("/in/old")):
            with self._proxied() as ftp:
                op(ftp)
            _, read = _bridge.last_timeout
            self.assertIsNotNone(read)

    def test_remove_deletes_and_a_missing_file_is_error_perm_on_both(self):
        FakeFTP.files = {"/in/a.bin": [b"x"]}
        files, _ = self.assertSameWrite(lambda ftp: ftp.remove("/in/a.bin"))
        self.assertEqual(files, {})
        exc = self.assertSameWrite(lambda ftp: ftp.remove("/nope"), error_perm)
        self.assertTrue(str(exc).startswith("550"))

    def test_mkdir_accepts_an_existing_directory_on_both(self):
        _, dirs = self.assertSameWrite(lambda ftp: ftp.mkdir("/in/new"))
        self.assertEqual(dirs, {"/in/new"})
        FakeFTP.dirs = {"/in/new"}
        self.assertSameWrite(lambda ftp: ftp.mkdir("/in/new"))

    def test_rmdir_removes_and_a_missing_directory_is_error_perm_on_both(self):
        FakeFTP.dirs = {"/in/old"}
        _, dirs = self.assertSameWrite(lambda ftp: ftp.rmdir("/in/old"))
        self.assertEqual(dirs, set())
        self.assertSameWrite(lambda ftp: ftp.rmdir("/nope"), error_perm)

    def test_a_refused_login_on_a_write_is_tool_unreachable(self):
        FakeFTP.login_error = error_perm("530 Login incorrect")
        with self._proxied() as ftp, self.assertRaises(ToolUnreachable):
            ftp.put("/in/a.bin", b"x")
        self.assertEqual(FakeFTP.files, {})

    def test_a_write_answered_by_a_login_page_is_proxy_error(self):
        # A 200 that is not the write route's answer must never read as success.
        for data in (b"<html>login page</html>", b'{"files": []}'):
            with self.subTest(data):
                self._use(_canned(200, data))
                with self._proxied() as ftp, self.assertRaises(ProxyError):
                    ftp.upload("/in/a.bin", b"x")

    def test_an_unknown_write_op_is_bad_request(self):
        resp = self.flask.post(
            flask_proxy.URL_WRITE, json={"host": "h1", "op": "chmod", "remote_path": "/a"}
        )
        self.assertEqual(resp.status_code, 422)
        self.assertEqual(resp.get_json()["code"], "bad_request")

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

    def test_a_body_lost_after_the_headers_is_proxy_error(self):
        # The write may already have run; the caller must still get the
        # documented ProxyError, not a requests exception it does not expect.
        class Broken(FakeResponse):
            def json(self):
                raise proxy_client.requests.ConnectionError("connection reset")

        for status in (200, 422):
            with self.subTest(status):
                self._use(lambda *a, status=status, **k: Broken(status, b""))
                with self._proxied() as ftp, self.assertRaises(ProxyError):
                    ftp.remove("/in/a.bin")

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
    def test_methods_have_the_direct_clients_exact_signature(self):
        names = ("__init__", "home", "list_details", "download",
                 "upload", "put", "remove", "mkdir", "rmdir")
        for name in names:
            with self.subTest(name):
                self.assertEqual(
                    inspect.signature(getattr(proxy_client.FtpClient, name)),
                    inspect.signature(getattr(core_client.FtpClient, name)),
                )

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
