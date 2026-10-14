"""Non-UTF-8 filenames against a REAL ftplib session.

The other FTP suites patch ``FTP`` with a fake, which replaces exactly the
layer this bug lives in: ftplib decodes every control/data line with the
``encoding`` fixed at construction (default utf-8, strict), and encodes every
command the same way. A Korean Windows FTP server answers in cp949, so with the
default one Korean name in a directory sinks the whole NLST (UnicodeDecodeError
-> recorded as a listing failure) and ``RETR 한글.tif`` goes out as utf-8 bytes
the server reads as a missing file (550).

So this suite runs a tiny cp949-speaking FTP server on loopback and drives the
real ftplib through every transport: direct fleet, FtpClient, and the proxy
pair (client -> Flask blueprint -> FTP).

The second half covers what the office actually found (2026-10-14): the
non-ASCII names on the tools were not Korean at all but corrupted bytes, valid
in neither cp949 nor utf-8. No text codec can list those; latin-1 can, because
it maps every byte to one character and back.
"""

import socket
import socketserver
import threading
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

from ftp_handler.core.client import FtpClient
from ftp_handler.direct_downloader.fleet_downloader import (
    FtpFleetDownloader,
    HostSpec,
    ListDir,
)
from ftp_handler.proxy import flask_proxy as proxy_mod
from ftp_handler.proxy import proxy_downloader as client_mod

KOREAN = "/MEAS/한글.tif"
FILES = {KOREAN: b"korean-bytes", "/MEAS/plain.tif": b"plain-bytes"}


class _Cp949FtpHandler(socketserver.StreamRequestHandler):
    """Just enough FTP for login, PASV, NLST, SIZE, MDTM and RETR, in cp949."""

    files = FILES
    codec = "cp949"

    def _reply(self, line: str) -> None:
        self.wfile.write(f"{line}\r\n".encode(self.codec))

    def _data(self, payload: bytes) -> None:
        conn, _ = self._pasv.accept()
        with conn:
            conn.sendall(payload)
        self._pasv.close()

    def handle(self) -> None:
        try:
            self._serve()
        except OSError:
            pass  # the utf-8 client gives up mid-transfer; not this test's news

    def _serve(self) -> None:
        self._reply("220 cp949 server")
        for raw in self.rfile:
            # Strict on purpose: a client that sends utf-8 bytes fails here
            # exactly as the real server fails to find the file.
            try:
                cmd, _, arg = raw.decode(self.codec).rstrip("\r\n").partition(" ")
            except UnicodeDecodeError:
                self._reply("550 undecodable name")
                continue
            cmd = cmd.upper()
            if cmd == "USER":
                self._reply("331 password")
            elif cmd == "PASS":
                self._reply("230 ok")
            elif cmd in ("TYPE", "MDTM"):
                self._reply("200 ok" if cmd == "TYPE" else "502 no")
            elif cmd == "PASV":
                self._pasv = socket.create_server(("127.0.0.1", 0))
                port = self._pasv.getsockname()[1]
                self._reply(f"227 Entering (127,0,0,1,{port >> 8},{port & 255})")
            elif cmd == "NLST":
                self._reply("150 listing")
                self._data("\r\n".join(self.files).encode(self.codec) + b"\r\n")
                self._reply("226 done")
            elif cmd == "SIZE":
                if arg in self.files:
                    self._reply(f"213 {len(self.files[arg])}")
                else:
                    self._reply("550 no such file")
            elif cmd == "RETR":
                if arg not in self.files:
                    self._reply("550 no such file")
                    continue
                self._reply("150 sending")
                self._data(self.files[arg])
                self._reply("226 done")
            elif cmd == "QUIT":
                self._reply("221 bye")
                return
            else:
                self._reply("502 not implemented")


class _Server(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True


def _bridge(flask_client):
    """requests.post stand-in that routes to the Flask test client."""

    def fake_post(url, json=None, headers=None, timeout=None):
        resp = flask_client.post(urlsplit(url).path, json=json, headers=headers or {})

        class R:
            status_code = resp.status_code

            def raise_for_status(self):
                if resp.status_code >= 400:
                    raise client_mod.requests.HTTPError(resp.status_code)

            def json(self):
                return resp.get_json()

        return R()

    return fake_post


class Cp949FilenameTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = _Server(("127.0.0.1", 0), _Cp949FtpHandler)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def _spec(self):
        return [HostSpec("127.0.0.1", listings=[ListDir("/MEAS")])]

    def _direct(self, **kw):
        return FtpFleetDownloader(user="u", password="p", port=self.port, **kw)

    def test_default_utf8_cannot_list_a_cp949_directory(self):
        # Pins the failure the encoding knob exists for, so a test server
        # that silently stopped speaking cp949 can't make the rest vacuous.
        report = self._direct().list_dirs(self._spec())
        self.assertEqual(report.grouped(), {"127.0.0.1": []})
        self.assertEqual(len(report.failures), 1)
        self.assertIn("UnicodeDecodeError", report.failures[0].error)

    def test_direct_lists_and_downloads_korean_names(self):
        report = self._direct(encoding="cp949").download(self._spec())
        self.assertEqual(report.failures, [])
        self.assertEqual({f.remote_path: f.data for f in report.files}, FILES)

    def test_direct_sizes_korean_names(self):
        report = self._direct(encoding="cp949").size_dirs(self._spec())
        self.assertEqual(report.failures, [])
        self.assertEqual(
            {f.remote_path: f.size for f in report.files},
            {k: len(v) for k, v in FILES.items()},
        )

    def test_ftp_client_lists_and_downloads_korean_names(self):
        with FtpClient(
            host="127.0.0.1", user="u", password="p", port=self.port, encoding="cp949"
        ) as ftp:
            self.assertEqual(sorted(ftp.list_dir("/MEAS")), sorted(FILES))
            self.assertEqual(ftp.download(KOREAN), FILES[KOREAN])

    def test_proxy_forwards_encoding_to_the_ftp_side(self):
        fclient = proxy_mod.create_app().test_client()
        dl = client_mod.FtpFleetDownloader(
            user="u", password="p", port=self.port, encoding="cp949", client_workers=1
        )
        with patch.object(client_mod.requests, "post", _bridge(fclient)):
            report = dl.download(self._spec())
        self.assertEqual(report.failures, [])
        self.assertEqual({f.remote_path: f.data for f in report.files}, FILES)


# Valid in neither utf-8 (0xff, a bare 0x80) nor cp949 (0xff, 0x80 lead bytes).
GARBAGE_BYTES = b"/MEAS/\xff\x80\xc7\x8e\xa0\xfe\x81.tif"
GARBAGE = GARBAGE_BYTES.decode("latin-1")


class _RawBytesFtpHandler(_Cp949FtpHandler):
    """Same server, but names are raw bytes: the wire codec is latin-1, so the
    garbage name above goes out and comes back byte for byte."""

    files = {GARBAGE: b"junk-bytes", "/MEAS/plain.tif": b"plain-bytes"}
    codec = "latin-1"


class CorruptedFilenameTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = _Server(("127.0.0.1", 0), _RawBytesFtpHandler)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def _download(self, encoding):
        dl = FtpFleetDownloader(
            user="u", password="p", port=self.port, encoding=encoding
        )
        return dl.download([HostSpec("127.0.0.1", listings=[ListDir("/MEAS")])])

    def test_no_text_codec_can_list_a_corrupted_name(self):
        for encoding in ("utf-8", "cp949"):
            with self.subTest(encoding=encoding):
                report = self._download(encoding)
                self.assertEqual(report.files, [])
                self.assertIn("UnicodeDecodeError", report.failures[0].error)

    def test_latin1_lists_and_downloads_it_byte_for_byte(self):
        report = self._download("latin-1")
        self.assertEqual(report.failures, [])
        self.assertEqual(
            {f.remote_path: f.data for f in report.files}, _RawBytesFtpHandler.files
        )
        # The listed name re-encodes to exactly what the server holds.
        self.assertIn(GARBAGE_BYTES, {f.remote_path.encode("latin-1") for f in report.files})


if __name__ == "__main__":
    unittest.main()
