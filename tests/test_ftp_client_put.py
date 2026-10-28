"""FtpClient.put: no-clobber write via a temp name in the same folder + rename.

Same seam as test_ftp_client: ftp_handler.core.client.FTP is patched with a
stand-in. This one keeps a dict of remote files so each test can assert what
the server holds afterwards, and lets a test inject a failure at any command.
"""

import time
import unittest
from ftplib import error_perm
from unittest.mock import patch

from ftp_handler.core.client import (
    FtpClient,
    TargetExists,
    TransferTimeout,
    TransferUncertain,
)

FTP_PATCH_TARGET = "ftp_handler.core.client.FTP"


class StoreFTP:
    """Stand-in server: ``files`` is the remote tree, path -> bytes."""

    files: dict = {}
    closed = 0
    block = 8192  # the server's read size; small values make STOR multi-block
    pause = 0.0

    def __init__(self, timeout=None, encoding="utf-8"):
        pass

    def connect(self, host, port, timeout=None):
        pass

    def login(self, user, passwd):
        pass

    def set_pasv(self, value):
        pass

    def voidcmd(self, cmd):
        return "200 ok"

    def size(self, path):
        if path not in StoreFTP.files:
            raise error_perm(f"550 {path}: No such file")
        return len(StoreFTP.files[path])

    def storbinary(self, cmd, fp, blocksize=8192, callback=None):
        path = cmd.split(" ", 1)[1]
        StoreFTP.files[path] = b""
        while block := fp.read(StoreFTP.block):
            if StoreFTP.pause:
                time.sleep(StoreFTP.pause)
            StoreFTP.files[path] += block
            if callback is not None:
                callback(block)
        return "226 done"

    def rename(self, fromname, toname):
        # POSIX-like: replaces an existing target, as many servers do.
        StoreFTP.files[toname] = StoreFTP.files.pop(fromname)
        return "250 renamed"

    def delete(self, path):
        del StoreFTP.files[path]
        return "250 deleted"

    def close(self):
        StoreFTP.closed += 1


class PutTests(unittest.TestCase):
    def setUp(self) -> None:
        StoreFTP.files = {}
        StoreFTP.closed = 0
        StoreFTP.block = 8192
        StoreFTP.pause = 0.0
        patcher = patch(FTP_PATCH_TARGET, StoreFTP)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _client(self) -> FtpClient:
        return FtpClient(host="h1", user="u", password="p")

    def test_put_lands_the_bytes_at_the_target_and_leaves_no_temp(self):
        with self._client() as ftp:
            ftp.put("/rcp/new.rcp", b"recipe body")
        self.assertEqual(StoreFTP.files, {"/rcp/new.rcp": b"recipe body"})

    def test_put_refuses_to_overwrite_an_existing_target(self):
        StoreFTP.files = {"/rcp/a.rcp": b"original"}
        with self._client() as ftp:
            with self.assertRaises(TargetExists):
                ftp.put("/rcp/a.rcp", b"replacement")
        self.assertEqual(StoreFTP.files, {"/rcp/a.rcp": b"original"})

    def test_overwrite_replaces_an_existing_target_and_leaves_no_temp(self):
        StoreFTP.files = {"/rcp/a.rcp": b"original"}
        with self._client() as ftp:
            ftp.put("/rcp/a.rcp", b"replacement", overwrite=True)
        self.assertEqual(StoreFTP.files, {"/rcp/a.rcp": b"replacement"})

    def test_overwrite_of_a_new_target_just_writes_it(self):
        with self._client() as ftp:
            ftp.put("/rcp/new.rcp", b"body", overwrite=True)
        self.assertEqual(StoreFTP.files, {"/rcp/new.rcp": b"body"})

    def test_overwrite_deletes_the_target_before_the_rename(self):
        # Some servers refuse RNTO onto an existing file, so DELE goes first.
        StoreFTP.files = {"/rcp/a.rcp": b"original"}

        def refuse_onto_existing(self, fromname, toname):
            if toname in StoreFTP.files:
                raise error_perm("553 Target exists")
            StoreFTP.files[toname] = StoreFTP.files.pop(fromname)

        with patch.object(StoreFTP, "rename", refuse_onto_existing):
            with self._client() as ftp:
                ftp.put("/rcp/a.rcp", b"replacement", overwrite=True)
        self.assertEqual(StoreFTP.files, {"/rcp/a.rcp": b"replacement"})

    def test_overwrite_keeps_the_new_copy_when_rnto_fails_after_the_dele(self):
        # The old target is gone by then: deleting the temp too would leave
        # neither copy, so it stays and is named on the exception.
        StoreFTP.files = {"/rcp/a.rcp": b"original"}

        def refuse(self, fromname, toname):
            raise error_perm("553 Rename refused")

        with patch.object(StoreFTP, "rename", refuse):
            with self._client() as ftp:
                with self.assertRaises(error_perm) as caught:
                    ftp.put("/rcp/a.rcp", b"replacement", overwrite=True)
        temp = caught.exception.temp_path
        self.assertEqual(StoreFTP.files, {temp: b"replacement"})
        self.assertIn(f"temp file left on server: {temp}", caught.exception.__notes__)

    def test_overwrite_that_cannot_delete_the_target_keeps_it_and_drops_the_temp(self):
        StoreFTP.files = {"/rcp/a.rcp": b"original"}
        real_delete = StoreFTP.delete

        def refuse_target(self, path):
            if path == "/rcp/a.rcp":
                raise error_perm("550 Permission denied")
            return real_delete(self, path)

        with patch.object(StoreFTP, "delete", refuse_target):
            with self._client() as ftp:
                with self.assertRaises(error_perm):
                    ftp.put("/rcp/a.rcp", b"replacement", overwrite=True)
        self.assertEqual(StoreFTP.files, {"/rcp/a.rcp": b"original"})

    def test_overwrite_with_an_undecodable_dele_reply_keeps_the_new_copy(self):
        # The server ran the DELE; only its reply was unreadable. The old
        # target may be gone, so the temp must survive.
        StoreFTP.files = {"/rcp/a.rcp": b"original"}
        real_delete = StoreFTP.delete

        def dele_then_garble(self, path):
            real_delete(self, path)
            if path == "/rcp/a.rcp":
                raise UnicodeDecodeError("utf-8", b"\xb0", 0, 1, "bad byte")

        with patch.object(StoreFTP, "delete", dele_then_garble):
            with self._client() as ftp:
                with self.assertRaises(TransferUncertain) as caught:
                    ftp.put("/rcp/a.rcp", b"replacement", overwrite=True)
        self.assertEqual(StoreFTP.files, {caught.exception.temp_path: b"replacement"})

    def test_overwrite_with_a_lost_rnto_reply_is_transfer_uncertain(self):
        StoreFTP.files = {"/rcp/a.rcp": b"original"}

        def lost(self, fromname, toname):
            raise EOFError()

        with patch.object(StoreFTP, "rename", lost):
            with self._client() as ftp:
                with self.assertRaises(TransferUncertain) as caught:
                    ftp.put("/rcp/a.rcp", b"replacement", overwrite=True)
        self.assertEqual(StoreFTP.files, {caught.exception.temp_path: b"replacement"})

    def test_a_refused_rnto_removes_only_this_calls_temp_file(self):
        # Another writer's leftover temp sits beside ours; it is not ours to touch.
        StoreFTP.files = {"/rcp/a.rcp.0000.part": b"someone else", "/rcp/b.rcp": b"b"}

        def refuse(self, fromname, toname):
            raise error_perm("553 Rename refused")

        with patch.object(StoreFTP, "rename", refuse):
            with self._client() as ftp:
                with self.assertRaises(error_perm):
                    ftp.put("/rcp/a.rcp", b"new")
        self.assertEqual(
            StoreFTP.files, {"/rcp/a.rcp.0000.part": b"someone else", "/rcp/b.rcp": b"b"}
        )

    def test_a_temp_file_that_cannot_be_deleted_is_named_on_the_exception(self):
        def refuse_rename(self, fromname, toname):
            raise error_perm("553 Rename refused")

        def refuse_delete(self, path):
            raise error_perm("550 Permission denied")

        with patch.object(StoreFTP, "rename", refuse_rename), patch.object(
            StoreFTP, "delete", refuse_delete
        ):
            with self._client() as ftp:
                with self.assertRaises(error_perm) as caught:
                    ftp.put("/rcp/a.rcp", b"new")
        # The original failure is what propagates, not the cleanup's.
        self.assertIn("553", str(caught.exception))
        [left] = StoreFTP.files
        self.assertEqual(caught.exception.temp_path, left)
        self.assertTrue(left.startswith("/rcp/a.rcp."))

    def test_a_lost_rnto_reply_raises_transfer_uncertain(self):
        # The server performs the rename, then the connection drops before the
        # 250 arrives: the caller cannot know which name the file now has.
        real_rename = StoreFTP.rename

        def rename_then_drop(self, fromname, toname):
            real_rename(self, fromname, toname)
            raise ConnectionResetError("connection reset by peer")

        with patch.object(StoreFTP, "rename", rename_then_drop):
            with self._client() as ftp:
                with self.assertRaises(TransferUncertain) as caught:
                    ftp.put("/rcp/a.rcp", b"new")
                self.assertEqual(StoreFTP.closed, 1)  # dead connection discarded
        self.assertTrue(caught.exception.temp_path.startswith("/rcp/a.rcp."))
        self.assertEqual(StoreFTP.files, {"/rcp/a.rcp": b"new"})

    def test_a_trickling_stor_is_cut_off_at_its_deadline(self):
        StoreFTP.block = 1
        StoreFTP.pause = 0.02
        with self._client() as ftp:
            with self.assertRaises(TransferTimeout) as caught:
                ftp.put("/rcp/a.rcp", b"x" * 20, deadline=0.05)
            # Mid-transfer, the control channel is out of step: discarded, so
            # the partial temp cannot be deleted and is reported instead.
            self.assertEqual(StoreFTP.closed, 1)
        [left] = StoreFTP.files
        self.assertLess(len(StoreFTP.files[left]), 20)
        self.assertEqual(caught.exception.temp_path, left)
        self.assertNotIn("/rcp/a.rcp", StoreFTP.files)

    def test_a_writer_that_lands_the_target_during_our_stor_is_not_clobbered(self):
        real_stor = StoreFTP.storbinary

        def stor_while_another_writer_lands(self, cmd, fp, blocksize=8192, callback=None):
            StoreFTP.files["/rcp/a.rcp"] = b"other writer"
            return real_stor(self, cmd, fp, blocksize, callback)

        with patch.object(StoreFTP, "storbinary", stor_while_another_writer_lands):
            with self._client() as ftp:
                with self.assertRaises(TargetExists):
                    ftp.put("/rcp/a.rcp", b"ours")
        self.assertEqual(StoreFTP.files, {"/rcp/a.rcp": b"other writer"})

    def test_known_limit_a_writer_landing_between_last_check_and_rnto_is_clobbered(self):
        # Documents the best-effort guarantee rather than desired behaviour:
        # ftplib's rename has no no-clobber option, so on a server whose RNTO
        # replaces an existing file (as this stand-in does) the last writer
        # wins. Single-writer callers never hit this window.
        real_rename = StoreFTP.rename

        def another_writer_lands_first(self, fromname, toname):
            StoreFTP.files[toname] = b"other writer"
            return real_rename(self, fromname, toname)

        with patch.object(StoreFTP, "rename", another_writer_lands_first):
            with self._client() as ftp:
                ftp.put("/rcp/a.rcp", b"ours")
        self.assertEqual(StoreFTP.files, {"/rcp/a.rcp": b"ours"})


if __name__ == "__main__":
    unittest.main()
