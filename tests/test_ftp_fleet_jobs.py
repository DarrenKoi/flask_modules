"""Unit tests for the non-blocking background job runner (web_app.jobs).

No live FTP and no real web server: the FTP layer is patched with the same
FakeFTP used by the fleet downloader tests, and the work submitted to
BackgroundJobs is a plain ``download`` call against it. Asserts the behaviors a
web app depends on: submit() returns at once, the work runs and lands a result
on a background thread, exceptions become an error job (never crash the caller),
status serialization carries counts but never file bytes, and the registry
evicts old finished jobs.
"""

import threading
import time
import unittest
from unittest.mock import patch

from ftp_handler.direct_downloader.fleet_downloader import (
    DownloadReport,
    FtpFleetDownloader,
    HostSpec,
)
from ftp_handler.web_app.jobs import BackgroundJobs, job_to_dict, summarize

# Reuse the fleet downloader's FakeFTP so a "job" exercises a real download.
from tests.test_ftp_fleet_downloader import FakeFTP, FTP_PATCH_TARGET


def _await_done(jobs: BackgroundJobs, job_id: str, timeout: float = 2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        snap = jobs.get(job_id)
        if snap is not None and snap.status != "running":
            return snap
        time.sleep(0.005)
    raise AssertionError(f"job {job_id} did not finish within {timeout}s")


class BackgroundJobsTests(unittest.TestCase):
    def setUp(self) -> None:
        FakeFTP.scripts = {}
        self.jobs = BackgroundJobs()

    def tearDown(self) -> None:
        self.jobs.shutdown()
        FakeFTP.scripts = {}

    def test_submit_returns_immediately_without_blocking(self):
        # The work blocks on an event we never set; submit() must still return
        # at once with the job left "running".
        gate = threading.Event()
        job_id = self.jobs.submit(lambda: gate.wait(5))
        snap = self.jobs.get(job_id)
        self.assertEqual(snap.status, "running")
        self.assertIsNone(snap.finished_at)
        gate.set()  # let the worker finish so teardown's shutdown is clean

    def test_download_runs_on_background_thread_and_lands_result(self):
        FakeFTP.scripts = {"h1": {"files": {"/log.txt": b"hello"}}}
        with patch(FTP_PATCH_TARGET, FakeFTP):
            dl = FtpFleetDownloader(user="u", password="p")
            job_id = self.jobs.submit(
                lambda: dl.download([HostSpec("h1", files=["/log.txt"])])
            )
            snap = _await_done(self.jobs, job_id)
        self.assertEqual(snap.status, "done")
        self.assertIsNone(snap.error)
        self.assertIsInstance(snap.result, DownloadReport)
        self.assertEqual(snap.result.ok, 1)

    def test_exception_in_work_becomes_error_job(self):
        def boom():
            raise RuntimeError("kaboom")

        job_id = self.jobs.submit(boom)
        snap = _await_done(self.jobs, job_id)
        self.assertEqual(snap.status, "error")
        self.assertIsNone(snap.result)
        self.assertIn("kaboom", snap.error)

    def test_get_unknown_job_returns_none(self):
        self.assertIsNone(self.jobs.get("does-not-exist"))

    def test_get_returns_a_snapshot_not_the_live_job(self):
        # Mutating the worker's job later must not change a snapshot already read.
        gate = threading.Event()
        job_id = self.jobs.submit(lambda: gate.wait(5))
        snap = self.jobs.get(job_id)
        gate.set()
        _await_done(self.jobs, job_id)
        self.assertEqual(snap.status, "running")  # the earlier snapshot is frozen


class SerializationTests(unittest.TestCase):
    def test_summarize_download_report_drops_bytes(self):
        from ftp_handler.direct_downloader.fleet_downloader import FileResult, HostFailure

        report = DownloadReport(
            files=[FileResult("h1", "/a", b"secret-bytes")],
            failures=[HostFailure("h2", "TimeoutError", None)],
        )
        out = summarize(report)
        self.assertEqual(out["ok"], 1)
        self.assertEqual(out["ng"], 1)
        self.assertEqual(out["failure_ratio"], 0.5)
        self.assertEqual(out["failures"], [{"host": "h2", "error": "TimeoutError", "remote_path": None}])
        # No raw file bytes anywhere in the serialized form.
        self.assertNotIn("secret-bytes", repr(out))

    def test_summarize_non_report_returns_none(self):
        self.assertIsNone(summarize({"not": "a report"}))
        self.assertIsNone(summarize(None))

    def test_job_to_dict_running_has_no_result(self):
        jobs = BackgroundJobs()
        gate = threading.Event()
        try:
            job_id = jobs.submit(lambda: gate.wait(5))
            d = job_to_dict(jobs.get(job_id))
            self.assertEqual(d["status"], "running")
            self.assertIsNone(d["result"])
            self.assertIsNone(d["finished_at"])
        finally:
            gate.set()
            jobs.shutdown()


class EvictionTests(unittest.TestCase):
    def test_old_finished_jobs_are_evicted_past_keep_last(self):
        # Await each job before submitting the next so every prior job is
        # finished when the next submit triggers eviction — deterministic order.
        jobs = BackgroundJobs(keep_last=3)
        try:
            ids = []
            for _ in range(5):
                job_id = jobs.submit(lambda: None)
                _await_done(jobs, job_id)
                ids.append(job_id)
            present = [job_id for job_id in ids if jobs.get(job_id) is not None]
            self.assertEqual(len(present), 3)        # capped at keep_last
            self.assertIsNone(jobs.get(ids[0]))      # oldest evicted
            self.assertIsNotNone(jobs.get(ids[-1]))  # newest survives
        finally:
            jobs.shutdown()


if __name__ == "__main__":
    unittest.main()
