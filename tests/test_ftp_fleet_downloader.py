"""Unit tests for the FTP fleet downloader and its archive→parse→index glue.

No live FTP server: ``ftp_handler.ftp_fleet_downloader.FTP`` is patched with
FakeFTP, whose per-host behavior is driven by a class-level script the test
sets up. Asserts the behaviors the design hinges on: per-host error isolation,
both discovery modes, on_file streaming, threshold math, and the disk helper.
"""

import socket
import tempfile
import unittest
from ftplib import error_perm
from pathlib import Path
from unittest.mock import patch

from ftp_handler.direct_downloader.collect import build_host_specs, collect_fleet
from ftp_handler.direct_downloader.fleet_downloader import (
    DownloadReport,
    FtpFleetDownloader,
    HostSpec,
    ListDir,
    ListingReport,
    SizingReport,
    UploadFile,
    UploadReport,
    UploadSpec,
    _keep_last_components,
    _safe_relative,
    download_fleet,
    group_files_by_host,
    list_fleet,
    put_bytes_to_minio,
    put_parquet_to_minio,
    put_pickle_to_minio,
    save_to_dir,
    size_fleet,
    specs_from_hosts,
    upload_fleet,
    upload_specs_from_hosts,
)

FTP_PATCH_TARGET = "ftp_handler.direct_downloader.fleet_downloader.FTP"


class FakeFTP:
    """Stand-in for ftplib.FTP. Per-host behavior comes from FakeFTP.scripts::

        FakeFTP.scripts = {
            "host": {
                "connect_error": Exception | None,
                "login_error": Exception | None,
                "files": {remote_path: bytes | Exception},
                "listing": {remote_dir: list[str] | Exception},
            }
        }
    """

    scripts: dict = {}

    def __init__(self, timeout=None):
        self.timeout = timeout
        self.host = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def _script(self) -> dict:
        return FakeFTP.scripts.get(self.host, {})

    def connect(self, host, port, timeout=None):
        self.host = host
        err = FakeFTP.scripts.get(host, {}).get("connect_error")
        if err is not None:
            raise err

    def login(self, user, passwd):
        err = self._script().get("login_error")
        if err is not None:
            raise err

    def set_pasv(self, value):
        self.passive = value

    def nlst(self, remote_dir):
        listing = self._script().get("listing", {})
        if remote_dir not in listing:
            raise error_perm(f"550 No such directory: {remote_dir}")
        value = listing[remote_dir]
        if isinstance(value, Exception):
            raise value
        return value

    def retrbinary(self, cmd, callback):
        remote_path = cmd.split(" ", 1)[1]
        files = self._script().get("files", {})
        if remote_path not in files:
            raise error_perm(f"550 No such file: {remote_path}")
        value = files[remote_path]
        if isinstance(value, Exception):
            raise value
        callback(value)

    def storbinary(self, cmd, fp):
        # STOR records the bytes into the host's `stored` dict so a test can
        # assert what landed. A path listed in `store_errors` raises instead, to
        # exercise per-file upload failure isolation.
        remote_path = cmd.split(" ", 1)[1]
        err = self._script().get("store_errors", {}).get(remote_path)
        if err is not None:
            raise err
        self._script().setdefault("stored", {})[remote_path] = fp.read()

    def voidcmd(self, cmd):
        # size_dirs issues `TYPE I` before sizing; nothing to do for the fake.
        return "200 ok"

    def size(self, remote_path):
        # An explicit `sizes` dict wins (lets a test inject None/Exception to
        # exercise unsupported-SIZE and per-file failure paths); otherwise the
        # size is derived from the file's bytes, so the same script that drives
        # download also drives sizing.
        sizes = self._script().get("sizes", {})
        if remote_path in sizes:
            value = sizes[remote_path]
            if isinstance(value, Exception):
                raise value
            return value
        files = self._script().get("files", {})
        if remote_path in files:
            value = files[remote_path]
            if isinstance(value, Exception):
                raise value
            return len(value)
        raise error_perm(f"550 No such file: {remote_path}")


class _FakeFTPTestCase(unittest.TestCase):
    """Resets the shared FakeFTP script around every test."""

    def setUp(self) -> None:
        FakeFTP.scripts = {}

    def tearDown(self) -> None:
        FakeFTP.scripts = {}

    def _run(self, specs, **kwargs) -> DownloadReport:
        with patch(FTP_PATCH_TARGET, FakeFTP):
            dl = FtpFleetDownloader(user="u", password="p", **kwargs)
            return dl.download(specs)

    def _list(self, specs, **kwargs) -> ListingReport:
        with patch(FTP_PATCH_TARGET, FakeFTP):
            dl = FtpFleetDownloader(user="u", password="p", **kwargs)
            return dl.list_dirs(specs)

    def _upload(self, specs, **kwargs) -> UploadReport:
        with patch(FTP_PATCH_TARGET, FakeFTP):
            dl = FtpFleetDownloader(user="u", password="p", **kwargs)
            return dl.upload(specs)

    def _size(self, specs, **kwargs) -> SizingReport:
        with patch(FTP_PATCH_TARGET, FakeFTP):
            dl = FtpFleetDownloader(user="u", password="p", **kwargs)
            return dl.size_dirs(specs)


class FtpFleetDownloaderTests(_FakeFTPTestCase):
    def test_fixed_path_download_returns_bytes(self):
        FakeFTP.scripts = {"h1": {"files": {"/log.txt": b"hello"}}}
        report = self._run([HostSpec("h1", files=["/log.txt"])])

        self.assertEqual(report.ok, 1)
        self.assertEqual(report.ng, 0)
        self.assertEqual(report.files[0].host, "h1")
        self.assertEqual(report.files[0].remote_path, "/log.txt")
        self.assertEqual(report.files[0].data, b"hello")

    def test_per_host_error_isolation(self):
        # h1's connect blows up; h2 must still download. One dead host never
        # aborts the rest of the fleet.
        FakeFTP.scripts = {
            "h1": {"connect_error": socket.timeout("timed out")},
            "h2": {"files": {"/log.txt": b"ok"}},
        }
        report = self._run(
            [HostSpec("h1", files=["/log.txt"]), HostSpec("h2", files=["/log.txt"])]
        )

        self.assertEqual(report.ok, 1)
        self.assertEqual(report.ng, 1)
        self.assertEqual(report.grouped(), {"h2": {"/log.txt": b"ok"}})
        failed = report.failures[0]
        self.assertEqual(failed.host, "h1")
        self.assertIsNone(failed.remote_path)  # failed before any file (connect)

    def test_listing_pattern_filters(self):
        FakeFTP.scripts = {
            "h1": {
                "listing": {"/MEAS": ["a.dat", "b.txt", "c.dat"]},
                "files": {"/MEAS/a.dat": b"A", "/MEAS/c.dat": b"C"},
            }
        }
        report = self._run([HostSpec("h1", listings=[ListDir("/MEAS", "*.dat")])])

        got = report.grouped()["h1"]
        self.assertEqual(set(got), {"/MEAS/a.dat", "/MEAS/c.dat"})
        self.assertEqual(got["/MEAS/a.dat"], b"A")

    def test_listing_returns_full_paths_normalized(self):
        # Some FTP servers return full paths from NLST; RETR must still work.
        FakeFTP.scripts = {
            "h1": {
                "listing": {"/MEAS": ["/MEAS/x.dat"]},
                "files": {"/MEAS/x.dat": b"X"},
            }
        }
        report = self._run([HostSpec("h1", listings=[ListDir("/MEAS", "*.dat")])])
        self.assertEqual(report.grouped(), {"h1": {"/MEAS/x.dat": b"X"}})

    def test_fixed_and_listing_combine_on_one_connection(self):
        FakeFTP.scripts = {
            "h1": {
                "listing": {"/MEAS": ["m1.dat"]},
                "files": {"/log.txt": b"L", "/MEAS/m1.dat": b"M"},
            }
        }
        report = self._run(
            [HostSpec("h1", files=["/log.txt"], listings=[ListDir("/MEAS", "*.dat")])]
        )
        self.assertEqual(
            report.grouped(), {"h1": {"/log.txt": b"L", "/MEAS/m1.dat": b"M"}}
        )

    def test_listing_failure_isolated_from_fixed_files(self):
        # The directory listing fails, but the fixed-path file on the same host
        # still downloads. The listing failure is recorded separately.
        FakeFTP.scripts = {
            "h1": {
                "listing": {},  # /MEAS not present -> nlst raises error_perm
                "files": {"/log.txt": b"L"},
            }
        }
        report = self._run(
            [HostSpec("h1", files=["/log.txt"], listings=[ListDir("/MEAS", "*.dat")])]
        )
        self.assertEqual(report.grouped(), {"h1": {"/log.txt": b"L"}})
        self.assertEqual(report.ng, 1)
        self.assertEqual(report.failures[0].remote_path, "/MEAS")

    def test_missing_file_recorded_per_file(self):
        FakeFTP.scripts = {"h1": {"files": {"/exists": b"y"}}}
        report = self._run([HostSpec("h1", files=["/exists", "/missing"])])

        self.assertEqual(report.ok, 1)
        self.assertEqual(report.ng, 1)
        self.assertEqual(report.failures[0].remote_path, "/missing")

    def test_on_file_streaming_drops_bytes(self):
        FakeFTP.scripts = {"h1": {"files": {"/a": b"A", "/b": b"B"}}}
        seen: list = []

        def on_file(host, remote_path, data):
            seen.append((host, remote_path, data))

        with patch(FTP_PATCH_TARGET, FakeFTP):
            dl = FtpFleetDownloader(user="u", password="p")
            report = dl.download(
                [HostSpec("h1", files=["/a", "/b"])], on_file=on_file
            )

        # Callback saw the real bytes...
        self.assertEqual(
            {(h, p, d) for h, p, d in seen},
            {("h1", "/a", b"A"), ("h1", "/b", b"B")},
        )
        # ...but the report retained none (RAM stays bounded).
        self.assertEqual(report.ok, 2)
        self.assertTrue(all(f.data == b"" for f in report.files))
        self.assertEqual(report.grouped(), {"h1": {"/a": b"", "/b": b""}})

    def test_on_file_exception_marks_that_file_failed(self):
        FakeFTP.scripts = {"h1": {"files": {"/good": b"G", "/bad": b"B"}}}

        def on_file(host, remote_path, data):
            if remote_path == "/bad":
                raise RuntimeError("index write failed")

        with patch(FTP_PATCH_TARGET, FakeFTP):
            dl = FtpFleetDownloader(user="u", password="p")
            report = dl.download(
                [HostSpec("h1", files=["/good", "/bad"])], on_file=on_file
            )

        self.assertEqual(report.ok, 1)
        self.assertEqual(report.ng, 1)
        self.assertEqual(report.failures[0].remote_path, "/bad")
        self.assertIn("index write failed", report.failures[0].error)

    def test_failure_ratio_math(self):
        FakeFTP.scripts = {
            "h1": {"files": {"/a": b"A"}},
            "h2": {"connect_error": socket.timeout("x")},
            "h3": {"connect_error": socket.timeout("x")},
        }
        report = self._run(
            [
                HostSpec("h1", files=["/a"]),
                HostSpec("h2", files=["/a"]),
                HostSpec("h3", files=["/a"]),
            ]
        )
        self.assertEqual(report.ok, 1)
        self.assertEqual(report.ng, 2)
        self.assertAlmostEqual(report.failure_ratio, 2 / 3)

    def test_empty_report_failure_ratio_is_zero(self):
        self.assertEqual(DownloadReport(files=[], failures=[]).failure_ratio, 0.0)

    def test_host_timeout_is_per_host_from_start(self):
        # A host whose worker runs longer than host_timeout must fail — even when
        # it runs concurrently with another and finishes while we're blocked on a
        # sibling. host_timeout is measured from each worker's start, so neither
        # can "borrow" budget from time spent waiting on the other.
        import time

        orig = FtpFleetDownloader._host_worker

        def slow(self, spec, on_file):
            time.sleep(0.15)
            return orig(self, spec, on_file)

        FakeFTP.scripts = {"h1": {"files": {"/f": b"a"}}, "h2": {"files": {"/f": b"b"}}}
        with patch(FTP_PATCH_TARGET, FakeFTP), patch.object(
            FtpFleetDownloader, "_host_worker", slow
        ):
            dl = FtpFleetDownloader(
                user="u", password="p", max_concurrency=2, host_timeout=0.1
            )
            report = dl.download(
                [HostSpec("h1", files=["/f"]), HostSpec("h2", files=["/f"])]
            )

        self.assertEqual(report.ok, 0)
        self.assertEqual(report.ng, 2)
        self.assertTrue(all("host_timeout" in f.error for f in report.failures))

    def test_download_fleet_helper(self):
        FakeFTP.scripts = {"h1": {"files": {"/log": b"data"}}}
        with patch(FTP_PATCH_TARGET, FakeFTP):
            report = download_fleet(
                [HostSpec("h1", files=["/log"])],
                user="u",
                password="p",
                max_concurrency=4,
            )
        self.assertEqual(report.grouped(), {"h1": {"/log": b"data"}})


class CollectFleetTests(_FakeFTPTestCase):
    def test_build_host_specs_maps_files_and_listings(self):
        fleet = [
            {
                "host": "10.0.0.1",
                "files": ["/log.txt"],
                "listings": [{"remote_dir": "/MEAS", "pattern": "*.dat"}],
            },
            {"host": "10.0.0.2"},  # no files / listings
        ]
        specs = build_host_specs(fleet)

        self.assertEqual(specs[0].host, "10.0.0.1")
        self.assertEqual(specs[0].files, ["/log.txt"])
        self.assertEqual(specs[0].listings, [ListDir("/MEAS", "*.dat")])
        self.assertEqual(specs[1].host, "10.0.0.2")
        self.assertEqual(specs[1].files, [])
        self.assertEqual(specs[1].listings, [])

    def test_collect_fleet_archives_parses_and_indexes_in_order(self):
        FakeFTP.scripts = {"h1": {"files": {"/a": b"raw-a"}}}
        calls: list = []
        indexed: list = []

        def archive(host, remote_path, data):
            calls.append("archive")
            return f"bucket/{host}{remote_path}"

        def parse(host, remote_path, data):
            calls.append("parse")
            return [{"host": host, "raw_len": len(data)}]

        def index(docs):
            calls.append("index")
            indexed.extend(docs)

        with patch(FTP_PATCH_TARGET, FakeFTP):
            report = collect_fleet(
                [HostSpec("h1", files=["/a"])],
                user="u",
                password="p",
                archive=archive,
                parse=parse,
                index=index,
            )

        self.assertEqual(report.ok, 1)
        # archive strictly before parse before index, per the agreed strictness.
        self.assertEqual(calls, ["archive", "parse", "index"])
        # minio_key stamped onto every doc.
        self.assertEqual(
            indexed, [{"host": "h1", "raw_len": 5, "minio_key": "bucket/h1/a"}]
        )

    def test_collect_fleet_index_failure_marks_file_failed(self):
        # If OpenSearch indexing throws, that file is a failure (archive
        # succeeded but the unit isn't done) — siblings are unaffected.
        FakeFTP.scripts = {"h1": {"files": {"/a": b"A", "/b": b"B"}}}

        def archive(host, remote_path, data):
            return "k"

        def parse(host, remote_path, data):
            return [{"p": remote_path}]

        def index(docs):
            if docs[0]["p"] == "/b":
                raise RuntimeError("opensearch down")

        with patch(FTP_PATCH_TARGET, FakeFTP):
            report = collect_fleet(
                [HostSpec("h1", files=["/a", "/b"])],
                user="u",
                password="p",
                archive=archive,
                parse=parse,
                index=index,
            )

        self.assertEqual(report.ok, 1)
        self.assertEqual(report.ng, 1)
        self.assertEqual(report.failures[0].remote_path, "/b")
        self.assertIn("opensearch down", report.failures[0].error)


class SafeRelativeTests(unittest.TestCase):
    def test_posix_path(self):
        self.assertEqual(
            _safe_relative("/HITACHI/SYSFILE/LOG.log"),
            Path("HITACHI/SYSFILE/LOG.log"),
        )

    def test_windows_ftp_backslashes(self):
        # A Windows-hosted FTP server may return backslash-separated paths.
        self.assertEqual(_safe_relative("\\MEAS\\sub\\x.dat"), Path("MEAS/sub/x.dat"))

    def test_strips_traversal_and_drive(self):
        # No escaping the dest dir; a Windows drive letter's colon is sanitized.
        self.assertEqual(_safe_relative("/../../etc/passwd"), Path("etc/passwd"))
        self.assertEqual(_safe_relative("C:/data/x"), Path("C_/data/x"))

    def test_sanitizes_illegal_chars(self):
        # A Linux filename with chars illegal on Windows must not crash a write.
        self.assertEqual(_safe_relative("/m/a:b?c.dat"), Path("m/a_b_c.dat"))

    def test_empty_falls_back(self):
        self.assertEqual(_safe_relative("/"), Path("_unnamed"))


class KeepLastComponentsTests(unittest.TestCase):
    def test_keeps_trailing_components(self):
        rel = Path("IMAGES/20260615/sub/x.jpeg")
        self.assertEqual(_keep_last_components(rel, 2), Path("sub/x.jpeg"))

    def test_keep_last_one_is_filename(self):
        self.assertEqual(
            _keep_last_components(Path("a/b/c.dat"), 1), Path("c.dat")
        )

    def test_keep_last_at_or_over_depth_returns_whole(self):
        rel = Path("a/b.dat")
        self.assertEqual(_keep_last_components(rel, 2), rel)
        self.assertEqual(_keep_last_components(rel, 9), rel)


class SaveToDirTests(_FakeFTPTestCase):
    def setUp(self) -> None:
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()
        super().tearDown()

    def test_save_to_dir_writes_files(self):
        FakeFTP.scripts = {
            "10.0.0.1": {"files": {"/HITACHI/SYSFILE/LOG.log": b"L"}},
            "10.0.0.2": {"files": {"/MEAS/x.dat": b"X"}},
        }
        with patch(FTP_PATCH_TARGET, FakeFTP):
            dl = FtpFleetDownloader(user="u", password="p")
            report = dl.download(
                [
                    HostSpec("10.0.0.1", files=["/HITACHI/SYSFILE/LOG.log"]),
                    HostSpec("10.0.0.2", files=["/MEAS/x.dat"]),
                ],
                on_file=save_to_dir(self.tmp_path),
            )

        self.assertEqual(report.ok, 2)
        self.assertEqual(
            (self.tmp_path / "10.0.0.1" / "HITACHI" / "SYSFILE" / "LOG.log").read_bytes(),
            b"L",
        )
        self.assertEqual(
            (self.tmp_path / "10.0.0.2" / "MEAS" / "x.dat").read_bytes(), b"X"
        )
        # Streaming write — report retains no bytes.
        self.assertTrue(all(f.data == b"" for f in report.files))

    def test_save_to_dir_keep_last_drops_parent_dirs(self):
        FakeFTP.scripts = {
            "10.0.0.1": {"files": {"/IMAGES/20260615/sub/S09-01AP.jpeg": b"J"}},
        }
        with patch(FTP_PATCH_TARGET, FakeFTP):
            dl = FtpFleetDownloader(user="u", password="p")
            report = dl.download(
                [HostSpec("10.0.0.1", files=["/IMAGES/20260615/sub/S09-01AP.jpeg"])],
                on_file=save_to_dir(self.tmp_path, keep_last=2),
            )

        self.assertEqual(report.ok, 1)
        # Only the trailing 2 components survive; /IMAGES/20260615 is dropped.
        self.assertEqual(
            (self.tmp_path / "10.0.0.1" / "sub" / "S09-01AP.jpeg").read_bytes(), b"J"
        )

    def test_save_to_dir_then_chains(self):
        FakeFTP.scripts = {"h1": {"files": {"/a": b"A"}}}
        chained: list = []
        with patch(FTP_PATCH_TARGET, FakeFTP):
            dl = FtpFleetDownloader(user="u", password="p")
            dl.download(
                [HostSpec("h1", files=["/a"])],
                on_file=save_to_dir(
                    self.tmp_path, then=lambda h, p, d: chained.append((h, p, d))
                ),
            )
        self.assertEqual((self.tmp_path / "h1" / "a").read_bytes(), b"A")
        self.assertEqual(chained, [("h1", "/a", b"A")])


class FakeMinio:
    """Stand-in for minio_handler.MinioObject. Records put / put_pickle /
    put_dataframe calls so the sink helpers can be tested without a live MinIO."""

    def __init__(self) -> None:
        self.objects: list[tuple[str, object]] = []
        self.pickles: list[tuple[str, object]] = []
        self.frames: list[tuple[str, object]] = []
        self.raise_on: str | None = None

    def put(self, key: str, data: object) -> None:
        if self.raise_on is not None and self.raise_on in key:
            raise RuntimeError("boom")
        self.objects.append((key, data))

    def put_pickle(self, key: str, obj: object) -> None:
        if self.raise_on is not None and self.raise_on in key:
            raise RuntimeError("boom")
        self.pickles.append((key, obj))

    def put_dataframe(self, key: str, df: object) -> None:
        self.frames.append((key, df))


class MinioSinkTests(_FakeFTPTestCase):
    def test_put_pickle_to_minio_transforms_and_keys_by_default(self):
        FakeFTP.scripts = {"10.0.0.1": {"files": {"/MEAS/x.dat": b"RAW"}}}
        mc = FakeMinio()
        with patch(FTP_PATCH_TARGET, FakeFTP):
            dl = FtpFleetDownloader(user="u", password="p")
            report = dl.download(
                [HostSpec("10.0.0.1", files=["/MEAS/x.dat"])],
                on_file=put_pickle_to_minio(
                    mc, lambda h, p, d: {"host": h, "n": len(d)}
                ),
            )

        self.assertEqual(report.ok, 1)
        self.assertEqual(
            mc.pickles, [("10.0.0.1/MEAS/x.dat.pkl", {"host": "10.0.0.1", "n": 3})]
        )
        # Streaming sink — report retains no bytes.
        self.assertTrue(all(f.data == b"" for f in report.files))

    def test_put_pickle_custom_key_and_then_chain(self):
        FakeFTP.scripts = {"h1": {"files": {"/a.dat": b"AB"}}}
        mc = FakeMinio()
        chained: list = []
        with patch(FTP_PATCH_TARGET, FakeFTP):
            FtpFleetDownloader(user="u", password="p").download(
                [HostSpec("h1", files=["/a.dat"])],
                on_file=put_pickle_to_minio(
                    mc,
                    lambda h, p, d: d.decode(),
                    key=lambda h, p: f"raw/{h}.pkl",
                    then=lambda h, p, d: chained.append((h, p, d)),
                ),
            )
        self.assertEqual(mc.pickles, [("raw/h1.pkl", "AB")])
        self.assertEqual(chained, [("h1", "/a.dat", b"AB")])

    def test_put_pickle_upload_failure_isolated_per_file(self):
        FakeFTP.scripts = {
            "h1": {"files": {"/good.dat": b"G", "/bad.dat": b"B"}},
        }
        mc = FakeMinio()
        mc.raise_on = "bad"
        with patch(FTP_PATCH_TARGET, FakeFTP):
            report = FtpFleetDownloader(user="u", password="p").download(
                [HostSpec("h1", files=["/good.dat", "/bad.dat"])],
                on_file=put_pickle_to_minio(mc, lambda h, p, d: d),
            )
        self.assertEqual(report.ok, 1)
        self.assertEqual(report.ng, 1)
        self.assertEqual(report.failures[0].remote_path, "/bad.dat")
        self.assertEqual(mc.pickles, [("h1/good.dat.pkl", b"G")])

    def test_put_bytes_to_minio_stores_raw_and_keys_by_default(self):
        FakeFTP.scripts = {"10.0.0.1": {"files": {"/MEAS/x.dat": b"RAW"}}}
        mc = FakeMinio()
        with patch(FTP_PATCH_TARGET, FakeFTP):
            report = FtpFleetDownloader(user="u", password="p").download(
                [HostSpec("10.0.0.1", files=["/MEAS/x.dat"])],
                on_file=put_bytes_to_minio(mc),
            )

        self.assertEqual(report.ok, 1)
        # Raw bytes, unchanged; default key mirrors <host>/<remote path>, no suffix.
        self.assertEqual(mc.objects, [("10.0.0.1/MEAS/x.dat", b"RAW")])
        # Streaming sink — report retains no bytes.
        self.assertTrue(all(f.data == b"" for f in report.files))

    def test_put_bytes_custom_key_and_then_chain(self):
        FakeFTP.scripts = {"h1": {"files": {"/a.dat": b"AB"}}}
        mc = FakeMinio()
        chained: list = []
        with patch(FTP_PATCH_TARGET, FakeFTP):
            FtpFleetDownloader(user="u", password="p").download(
                [HostSpec("h1", files=["/a.dat"])],
                on_file=put_bytes_to_minio(
                    mc,
                    key=lambda h, p: f"raw/{h}.bin",
                    then=lambda h, p, d: chained.append((h, p, d)),
                ),
            )
        self.assertEqual(mc.objects, [("raw/h1.bin", b"AB")])
        self.assertEqual(chained, [("h1", "/a.dat", b"AB")])

    def test_put_bytes_upload_failure_isolated_per_file(self):
        FakeFTP.scripts = {"h1": {"files": {"/good.dat": b"G", "/bad.dat": b"B"}}}
        mc = FakeMinio()
        mc.raise_on = "bad"
        with patch(FTP_PATCH_TARGET, FakeFTP):
            report = FtpFleetDownloader(user="u", password="p").download(
                [HostSpec("h1", files=["/good.dat", "/bad.dat"])],
                on_file=put_bytes_to_minio(mc),
            )
        self.assertEqual(report.ok, 1)
        self.assertEqual(report.ng, 1)
        self.assertEqual(report.failures[0].remote_path, "/bad.dat")
        self.assertEqual(mc.objects, [("h1/good.dat", b"G")])

    def test_put_parquet_to_minio_uses_dataframe_and_default_suffix(self):
        FakeFTP.scripts = {"h1": {"files": {"/m.dat": b"DATA"}}}
        mc = FakeMinio()
        sentinel = object()  # stand in for a DataFrame; helper just forwards it
        with patch(FTP_PATCH_TARGET, FakeFTP):
            FtpFleetDownloader(user="u", password="p").download(
                [HostSpec("h1", files=["/m.dat"])],
                on_file=put_parquet_to_minio(mc, lambda h, p, d: sentinel),
            )
        self.assertEqual(mc.frames, [("h1/m.dat.parquet", sentinel)])


class SpecBuildersTests(unittest.TestCase):
    def test_specs_from_hosts_shares_config(self):
        specs = specs_from_hosts(
            ["10.0.0.1", "10.0.0.2"], listings=[ListDir("/MEAS", "*.dat")]
        )
        self.assertEqual([s.host for s in specs], ["10.0.0.1", "10.0.0.2"])
        self.assertEqual(specs[0].listings, [ListDir("/MEAS", "*.dat")])
        self.assertEqual(specs[0].files, [])

    def test_specs_from_hosts_copies_lists_per_host(self):
        # Each spec must own its lists — mutating one never bleeds into another.
        specs = specs_from_hosts(["a", "b"], files=["/log"])
        specs[0].files.append("/extra")
        self.assertEqual(specs[1].files, ["/log"])

    def test_specs_from_hosts_empty_defaults(self):
        specs = specs_from_hosts(["a"])
        self.assertEqual(specs[0].files, [])
        self.assertEqual(specs[0].listings, [])

    def test_group_files_by_host_folds_pairs_per_host(self):
        # Same host across rows collapses to one spec; paths accumulate as files.
        specs = group_files_by_host(
            [
                ("10.0.0.1", "/a/1.idp"),
                ("10.0.0.2", "/b/2.idp"),
                ("10.0.0.1", "/a/3.idp"),
            ]
        )
        self.assertEqual([s.host for s in specs], ["10.0.0.1", "10.0.0.2"])
        self.assertEqual(specs[0].files, ["/a/1.idp", "/a/3.idp"])
        self.assertEqual(specs[1].files, ["/b/2.idp"])
        # files-only — this helper never produces listings.
        self.assertEqual(specs[0].listings, [])

    def test_group_files_by_host_preserves_first_appearance_order(self):
        specs = group_files_by_host(
            [("b", "/x"), ("a", "/y"), ("b", "/z")]
        )
        self.assertEqual([s.host for s in specs], ["b", "a"])

    def test_group_files_by_host_accepts_generator(self):
        # The DataFrame case feeds a generator, not a list — must consume it.
        rows = [
            ("10.0.0.1", "CLSA", "W1", "P100"),
            ("10.0.0.1", "CLSA", "W1", "P101"),
        ]
        specs = group_files_by_host(
            (ip, f"/HITACHI/DEVICE/HD/{cls}/data/{idw}/{idp}.idp")
            for ip, cls, idw, idp in rows
        )
        self.assertEqual(len(specs), 1)
        self.assertEqual(
            specs[0].files,
            [
                "/HITACHI/DEVICE/HD/CLSA/data/W1/P100.idp",
                "/HITACHI/DEVICE/HD/CLSA/data/W1/P101.idp",
            ],
        )

    def test_group_files_by_host_empty(self):
        self.assertEqual(group_files_by_host([]), [])


class ListDirsTests(_FakeFTPTestCase):
    def test_list_dirs_discovers_paths_without_fetching(self):
        # Files are present in the script but list_dirs must never RETR them.
        FakeFTP.scripts = {
            "h1": {
                "listing": {"/MEAS": ["a.dat", "b.txt", "c.dat"]},
                "files": {"/MEAS/a.dat": b"A"},
            }
        }
        report = self._list([HostSpec("h1", listings=[ListDir("/MEAS", "*.dat")])])

        self.assertEqual(report.ok, 1)
        self.assertEqual(report.ng, 0)
        self.assertEqual(report.grouped(), {"h1": ["/MEAS/a.dat", "/MEAS/c.dat"]})
        self.assertEqual(report.total_paths, 2)

    def test_list_dirs_ignores_fixed_files(self):
        # spec.files is for known paths; listing is for discovery — files stay out.
        FakeFTP.scripts = {"h1": {"listing": {"/MEAS": ["x.dat"]}}}
        report = self._list(
            [HostSpec("h1", files=["/known.log"], listings=[ListDir("/MEAS")])]
        )
        self.assertEqual(report.grouped(), {"h1": ["/MEAS/x.dat"]})

    def test_list_dirs_per_host_error_isolation(self):
        FakeFTP.scripts = {
            "h1": {"connect_error": socket.timeout("timed out")},
            "h2": {"listing": {"/MEAS": ["ok.dat"]}},
        }
        report = self._list(
            [
                HostSpec("h1", listings=[ListDir("/MEAS")]),
                HostSpec("h2", listings=[ListDir("/MEAS")]),
            ]
        )
        self.assertEqual(report.grouped(), {"h2": ["/MEAS/ok.dat"]})
        self.assertEqual(report.ng, 1)
        self.assertEqual(report.failures[0].host, "h1")
        self.assertIsNone(report.failures[0].remote_path)  # connect failed

    def test_list_dirs_listing_failure_isolated_per_dir(self):
        # One dir enumerates, a sibling dir fails; host still returns the good one.
        FakeFTP.scripts = {"h1": {"listing": {"/A": ["a.dat"]}}}  # /B absent -> error
        report = self._list([HostSpec("h1", listings=[ListDir("/A"), ListDir("/B")])])

        self.assertEqual(report.grouped(), {"h1": ["/A/a.dat"]})
        self.assertEqual(report.ng, 1)
        self.assertEqual(report.failures[0].remote_path, "/B")

    def test_to_specs_round_trips_into_download(self):
        # The loop-closer: list discovers paths, to_specs() makes them fixed
        # files, download fetches exactly those.
        FakeFTP.scripts = {
            "h1": {
                "listing": {"/MEAS": ["m1.dat", "m2.dat"]},
                "files": {"/MEAS/m1.dat": b"1", "/MEAS/m2.dat": b"2"},
            }
        }
        listing = self._list([HostSpec("h1", listings=[ListDir("/MEAS")])])
        specs = listing.to_specs()
        self.assertEqual(
            specs, [HostSpec("h1", files=["/MEAS/m1.dat", "/MEAS/m2.dat"])]
        )

        report = self._run(specs)
        self.assertEqual(
            report.grouped(), {"h1": {"/MEAS/m1.dat": b"1", "/MEAS/m2.dat": b"2"}}
        )

    def test_to_specs_drops_empty_hosts(self):
        FakeFTP.scripts = {"h1": {"listing": {"/MEAS": []}}}
        listing = self._list([HostSpec("h1", listings=[ListDir("/MEAS")])])
        self.assertEqual(listing.ok, 1)  # host connected and reported (empty) listing
        self.assertEqual(listing.to_specs(), [])  # but nothing to download

    def test_list_fleet_helper(self):
        FakeFTP.scripts = {"h1": {"listing": {"/MEAS": ["a.dat"]}}}
        with patch(FTP_PATCH_TARGET, FakeFTP):
            report = list_fleet(
                [HostSpec("h1", listings=[ListDir("/MEAS")])],
                user="u",
                password="p",
                max_concurrency=4,
            )
        self.assertEqual(report.grouped(), {"h1": ["/MEAS/a.dat"]})


class SizingTests(_FakeFTPTestCase):
    def test_sizes_fixed_paths_without_fetching(self):
        # SIZE only; the file bytes drive the fake's reported size but are never
        # RETR'd in a sizing pass.
        FakeFTP.scripts = {"h1": {"files": {"/log/a.log": b"AAAA", "/log/b.log": b"BB"}}}
        report = self._size([HostSpec("h1", files=["/log/a.log", "/log/b.log"])])
        self.assertEqual(report.ok, 2)
        self.assertEqual(report.ng, 0)
        self.assertEqual(report.total_bytes, 6)

    def test_sizes_discovered_paths_from_listings(self):
        FakeFTP.scripts = {
            "h1": {
                "listing": {"/MEAS": ["a.dat", "b.txt"]},
                "files": {"/MEAS/a.dat": b"1234567890"},
            }
        }
        report = self._size([HostSpec("h1", listings=[ListDir("/MEAS", "*.dat")])])
        self.assertEqual(report.total_bytes, 10)
        self.assertEqual([f.remote_path for f in report.files], ["/MEAS/a.dat"])

    def test_total_bytes_and_by_host_split(self):
        FakeFTP.scripts = {
            "h1": {"files": {"/a": b"AAA"}},          # 3 bytes
            "h2": {"files": {"/b": b"BBBBB", "/c": b"C"}},  # 6 bytes
        }
        report = self._size(
            [HostSpec("h1", files=["/a"]), HostSpec("h2", files=["/b", "/c"])]
        )
        self.assertEqual(report.total_bytes, 9)
        self.assertEqual(report.by_host(), {"h1": 3, "h2": 6})

    def test_per_file_size_failure_isolated(self):
        FakeFTP.scripts = {
            "h1": {"files": {"/ok": b"OK"}, "sizes": {"/bad": error_perm("550")}}
        }
        report = self._size([HostSpec("h1", files=["/ok", "/bad"])])
        self.assertEqual(report.total_bytes, 2)
        self.assertEqual(report.ng, 1)
        self.assertEqual(report.failures[0].remote_path, "/bad")

    def test_unsupported_size_recorded_not_counted(self):
        # ftplib returns None when the server lacks SIZE support — that file is a
        # failure, never silently summed as zero.
        FakeFTP.scripts = {"h1": {"sizes": {"/x": None}}}
        report = self._size([HostSpec("h1", files=["/x"])])
        self.assertEqual(report.ok, 0)
        self.assertEqual(report.ng, 1)
        self.assertIn("SIZE unsupported", report.failures[0].error)
        self.assertEqual(report.total_bytes, 0)

    def test_per_host_connect_error_isolation(self):
        FakeFTP.scripts = {
            "h1": {"files": {"/a": b"AAAA"}},
            "h2": {"connect_error": socket.timeout("dead")},
        }
        report = self._size(
            [HostSpec("h1", files=["/a"]), HostSpec("h2", files=["/a"])]
        )
        self.assertEqual(report.total_bytes, 4)
        self.assertEqual(report.ng, 1)
        self.assertEqual(report.failures[0].host, "h2")

    def test_to_specs_round_trips_into_download(self):
        FakeFTP.scripts = {
            "h1": {"listing": {"/MEAS": ["a.dat"]}, "files": {"/MEAS/a.dat": b"DATA"}}
        }
        sizing = self._size([HostSpec("h1", listings=[ListDir("/MEAS", "*.dat")])])
        report = self._run(sizing.to_specs())
        self.assertEqual(report.grouped(), {"h1": {"/MEAS/a.dat": b"DATA"}})

    def test_empty_report_failure_ratio_is_zero(self):
        report = self._size([])
        self.assertEqual(report.total_bytes, 0)
        self.assertEqual(report.failure_ratio, 0.0)

    def test_size_fleet_helper(self):
        FakeFTP.scripts = {"h1": {"files": {"/a": b"AAAA"}}}
        with patch(FTP_PATCH_TARGET, FakeFTP):
            report = size_fleet(
                [HostSpec("h1", files=["/a"])], user="u", password="p"
            )
        self.assertEqual(report.total_bytes, 4)


class UploadTests(_FakeFTPTestCase):
    def test_uploads_in_memory_bytes(self):
        # No disk file: raw bytes go straight to STOR via BytesIO. The fake
        # records what landed so we can assert the bytes round-tripped.
        FakeFTP.scripts = {"h1": {}}
        report = self._upload(
            [UploadSpec("h1", files=[UploadFile("/INBOX/r.csv", b"a,b\n1,2")])]
        )

        self.assertEqual(report.ok, 1)
        self.assertEqual(report.ng, 0)
        self.assertEqual(report.results[0].host, "h1")
        self.assertEqual(report.results[0].remote_path, "/INBOX/r.csv")
        self.assertEqual(FakeFTP.scripts["h1"]["stored"], {"/INBOX/r.csv": b"a,b\n1,2"})

    def test_per_host_error_isolation(self):
        # h1's connect fails; h2 still uploads. One dead host never sinks the rest.
        FakeFTP.scripts = {
            "h1": {"connect_error": socket.timeout("timed out")},
            "h2": {},
        }
        report = self._upload(
            [
                UploadSpec("h1", files=[UploadFile("/a", b"A")]),
                UploadSpec("h2", files=[UploadFile("/a", b"A")]),
            ]
        )

        self.assertEqual(report.ok, 1)
        self.assertEqual(report.ng, 1)
        self.assertEqual(report.grouped(), {"h2": ["/a"]})
        self.assertEqual(report.failures[0].host, "h1")
        self.assertIsNone(report.failures[0].remote_path)  # failed at connect

    def test_per_file_error_isolation(self):
        # One STOR fails; the host's other files still upload. The failure is
        # recorded against that specific path.
        FakeFTP.scripts = {
            "h1": {"store_errors": {"/bad": error_perm("550 denied")}}
        }
        report = self._upload(
            [
                UploadSpec(
                    "h1",
                    files=[UploadFile("/good", b"G"), UploadFile("/bad", b"B")],
                )
            ]
        )

        self.assertEqual(report.ok, 1)
        self.assertEqual(report.ng, 1)
        self.assertEqual(report.grouped(), {"h1": ["/good"]})
        self.assertEqual(report.failures[0].remote_path, "/bad")

    def test_empty_report_failure_ratio_is_zero(self):
        self.assertEqual(UploadReport(results=[], failures=[]).failure_ratio, 0.0)

    def test_upload_specs_from_hosts_shares_files_per_host(self):
        files = [UploadFile("/INBOX/r.csv", b"data")]
        specs = upload_specs_from_hosts(["a", "b"], files=files)
        self.assertEqual([s.host for s in specs], ["a", "b"])
        # Each spec owns its own list copy — mutating one never bleeds.
        specs[0].files.append(UploadFile("/extra", b"x"))
        self.assertEqual(len(specs[1].files), 1)

    def test_upload_fleet_helper(self):
        FakeFTP.scripts = {"h1": {}}
        with patch(FTP_PATCH_TARGET, FakeFTP):
            report = upload_fleet(
                [UploadSpec("h1", files=[UploadFile("/a", b"A")])],
                user="u",
                password="p",
                max_concurrency=4,
            )
        self.assertEqual(report.grouped(), {"h1": ["/a"]})


if __name__ == "__main__":
    unittest.main()
