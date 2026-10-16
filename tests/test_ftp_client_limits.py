"""FtpClient's browsing additions: home(), bounded download and bounded LIST.

Same seam as test_ftp_client: ftp_handler.core.client.FTP is patched with a
stand-in. This one delivers RETR/LIST payloads in chunks, optionally pausing
between them, so a limit has to trip inside the receive callback rather than
after the whole payload is buffered.
"""

import time
import unittest
from ftplib import error_perm
from unittest.mock import patch

from ftp_handler.core.client import (
    FtpClient,
    ListingTooLarge,
    TransferTimeout,
    TransferTooLarge,
)

FTP_PATCH_TARGET = "ftp_handler.core.client.FTP"


class ChunkedFTP:
    home = "/"
    files: dict = {}
    list_lines: dict = {}
    pause = 0.0
    delivered: list = []
    closed = 0

    def __init__(self, timeout=None, encoding="utf-8"):
        pass

    def connect(self, host, port, timeout=None):
        pass

    def login(self, user, passwd):
        pass

    def set_pasv(self, value):
        pass

    def pwd(self):
        return ChunkedFTP.home

    def retrbinary(self, cmd, callback):
        for chunk in ChunkedFTP.files[cmd.split(" ", 1)[1]]:
            if ChunkedFTP.pause:
                time.sleep(ChunkedFTP.pause)
            ChunkedFTP.delivered.append(len(chunk))
            callback(chunk)

    def retrlines(self, cmd, callback):
        for line in ChunkedFTP.list_lines.get(cmd.split(" ", 1)[1], []):
            if ChunkedFTP.pause:
                time.sleep(ChunkedFTP.pause)
            ChunkedFTP.delivered.append(len(line))
            callback(line)

    def close(self):
        ChunkedFTP.closed += 1


class BrowsingTests(unittest.TestCase):
    def setUp(self) -> None:
        ChunkedFTP.home = "/"
        ChunkedFTP.files = {}
        ChunkedFTP.list_lines = {}
        ChunkedFTP.pause = 0.0
        ChunkedFTP.delivered = []
        ChunkedFTP.closed = 0
        patcher = patch(FTP_PATCH_TARGET, ChunkedFTP)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _client(self) -> FtpClient:
        return FtpClient(host="h1", user="u", password="p")

    def test_a_rejected_login_closes_the_socket_it_opened(self):
        def refuse(self, user, passwd):
            raise error_perm("530 Login incorrect")

        with patch.object(ChunkedFTP, "login", refuse):
            with self.assertRaises(error_perm):
                with self._client():
                    pass
        self.assertEqual(ChunkedFTP.closed, 1)

    def test_home_reports_the_login_directory(self):
        ChunkedFTP.home = "/home/ftpuser"
        with self._client() as ftp:
            self.assertEqual(ftp.home(), "/home/ftpuser")

    def test_download_stops_mid_transfer_once_max_bytes_is_exceeded(self):
        ChunkedFTP.files = {"/log/a.txt": [b"x" * 10] * 5}
        with self._client() as ftp:
            with self.assertRaises(TransferTooLarge):
                ftp.download("/log/a.txt", max_bytes=25)
            self.assertEqual(ChunkedFTP.delivered, [10, 10, 10])
            self.assertEqual(ChunkedFTP.closed, 1)

    def test_download_at_exactly_max_bytes_succeeds(self):
        ChunkedFTP.files = {"/log/a.txt": [b"ab", b"cd"]}
        with self._client() as ftp:
            self.assertEqual(ftp.download("/log/a.txt", max_bytes=4), b"abcd")

    def test_a_trickling_download_is_cut_off_at_its_deadline(self):
        # Every chunk arrives well inside any socket timeout; only the
        # whole-transfer deadline can stop this.
        ChunkedFTP.files = {"/log/slow.txt": [b"x"] * 20}
        ChunkedFTP.pause = 0.02
        with self._client() as ftp:
            with self.assertRaises(TransferTimeout):
                ftp.download("/log/slow.txt", deadline=0.05)
            self.assertLess(len(ChunkedFTP.delivered), 20)
            self.assertEqual(ChunkedFTP.closed, 1)

    def test_list_details_flags_links_and_still_returns_them(self):
        ChunkedFTP.list_lines = {
            "/rcp": [
                "lrwxrwxrwx   1 owner group   11 May 21 10:30 cur -> /sys/etc",
                "-rw-r--r--   1 owner group  123 May 21 10:30 a.rcp",
                "05-21-26  10:30AM       <DIR>          old",
            ]
        }
        with self._client() as ftp:
            entries = ftp.list_details("/rcp")
        self.assertEqual(
            [(e.name, e.is_dir, e.is_link) for e in entries],
            [("cur", False, True), ("a.rcp", False, False), ("old", True, False)],
        )

    def test_unparseable_list_lines_still_count_toward_max_entries(self):
        # None of these parse, so a cap on parsed entries would never trip.
        ChunkedFTP.list_lines = {"/MEAS": ["total 0 garbage"] * 1000}
        with self._client() as ftp:
            with self.assertRaises(ListingTooLarge):
                ftp.list_details("/MEAS", max_entries=50)
            self.assertEqual(len(ChunkedFTP.delivered), 51)
            self.assertEqual(ChunkedFTP.closed, 1)

    def test_a_few_huge_list_lines_trip_max_raw_bytes(self):
        ChunkedFTP.list_lines = {"/MEAS": ["x" * 100] * 10}
        with self._client() as ftp:
            with self.assertRaises(ListingTooLarge):
                ftp.list_details("/MEAS", max_raw_bytes=250)
            self.assertEqual(len(ChunkedFTP.delivered), 3)

    def test_a_list_that_never_finishes_is_cut_off_at_its_deadline(self):
        ChunkedFTP.list_lines = {"/MEAS": ["total 0 garbage"] * 20}
        ChunkedFTP.pause = 0.02
        with self._client() as ftp:
            with self.assertRaises(TransferTimeout):
                ftp.list_details("/MEAS", deadline=0.05)
            self.assertLess(len(ChunkedFTP.delivered), 20)
            self.assertEqual(ChunkedFTP.closed, 1)


if __name__ == "__main__":
    unittest.main()
