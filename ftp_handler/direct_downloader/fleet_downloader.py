"""In-memory concurrent FTP fleet downloader.

Pulls files from many equipment FTP servers at once and hands the raw bytes
back to the caller. Async fan-out is an implementation detail: callers use the
plain synchronous ``download()`` method (or the ``download_fleet()`` helper) and
never touch a coroutine, so this drops into a non-async script or an Airflow
``PythonOperator`` unchanged.

Why threads, not aioftp:
  Each host is one short-lived FTP session that is almost entirely socket I/O,
  and Python releases the GIL during socket I/O. So a ``ThreadPoolExecutor``
  over blocking ``ftplib`` fans out N hosts concurrently with zero extra
  packages — no aioftp to pip-install into an Airflow venv, no version drift
  between your laptop and the worker. There is no event loop: ``download`` and
  ``list_dirs`` are plain synchronous calls, safe to invoke from a script, an
  Airflow task, or a Flask request handler / scheduler thread — even one that
  already runs its own asyncio loop.

Two failure modes this is built to survive:
  - One unreachable / black-holed host. ``connect_timeout`` bounds every
    blocking socket op, ``host_timeout`` backstops a whole pathological host,
    and per-host errors are isolated (gather never aborts siblings). A dead
    host is reported in ``failures``; the rest still download.
  - Resource exhaustion under fan-out. ``max_concurrency`` caps simultaneous
    connections (and, because files are held in memory, peak RAM ~=
    concurrency x file size). Without this cap, ~200 simultaneous connections
    blow past the worker's open-file limit and downloads silently fail.

Memory:
  ``download(specs)`` collects every file's bytes into the returned report —
  peak RAM is the SUM of all files. For a large fleet, pass an ``on_file``
  callback instead: each file is handed to the callback the moment it lands and
  then dropped, so peak RAM stays bounded by concurrency x file size. The
  callback runs inside the per-host worker thread, so multiple callbacks run
  concurrently — use thread-safe clients or construct them inside the callback.
"""

import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from contextlib import contextmanager
from dataclasses import dataclass, field
from ftplib import FTP, all_errors
from io import BytesIO
from pathlib import Path
from typing import Callable, Iterator, Protocol, runtime_checkable

# The shared NLST normalizer lives in core so both downloaders behave
# identically. Relative import in-package; bare fallback when copied out flat
# beside the proxy pair (the file then sits next to listing.py).
try:
    from ..core.listing import _normalize_listing
except ImportError:  # copied out flat, imported by bare name
    from listing import _normalize_listing

# Invoked once per successfully downloaded file: (host, remote_path, data).
OnFile = Callable[[str, str, bytes], None]


@dataclass(slots=True)
class ListDir:
    """Discover files to fetch by listing a remote directory.

    ``remote_dir`` is listed (NLST); entries whose basename matches ``pattern``
    (an fnmatch glob, e.g. ``"*.dat"``) are fetched. ``pattern=None`` fetches
    every entry. Use this for the timestamped measurement files whose names you
    can't know ahead of time.
    """

    remote_dir: str
    pattern: str | None = None


@dataclass(slots=True)
class HostSpec:
    """One equipment host and everything to pull from it in a run.

    ``files``    fixed remote paths fetched directly (RETR), no listing — for
                 the append-only logs at known paths.
    ``listings`` directories listed then filtered, one RETR per match — for the
                 timestamped measurement files.

    Both run over a single FTP connection that is opened once, reused for the
    listing and every RETR, then closed.
    """

    host: str
    files: list[str] = field(default_factory=list)
    listings: list[ListDir] = field(default_factory=list)


@dataclass(slots=True)
class FileResult:
    """A successfully downloaded file. ``data`` is empty when an ``on_file``
    callback consumed the bytes (streaming mode) — the entry then only records
    that the file succeeded."""

    host: str
    remote_path: str
    data: bytes


@dataclass(slots=True)
class UploadFile:
    """One file to push to a host: the remote destination path and its bytes.

    The mirror of a download's ``FileResult`` for the write direction — here the
    caller supplies ``data`` (a download returns it). ``remote_path`` is STOR'd
    verbatim and overwrites any existing file at that path.
    """

    remote_path: str
    data: bytes


@dataclass(slots=True)
class UploadSpec:
    """One equipment host and the files to push to it in a run.

    The upload counterpart to ``HostSpec``: ``files`` are uploaded over a single
    FTP connection that is opened once, reused for every STOR, then closed.
    There is no listing analogue — upload destinations are always explicit.
    """

    host: str
    files: list[UploadFile] = field(default_factory=list)


@dataclass(slots=True)
class UploadResult:
    """A successfully uploaded file. Unlike ``FileResult`` it carries no bytes —
    the caller already holds the source data; this only records that the STOR
    landed."""

    host: str
    remote_path: str


@dataclass(slots=True)
class HostFailure:
    """A failed download. ``remote_path`` is ``None`` when the failure happened
    before any specific file (connect / login / directory listing)."""

    host: str
    error: str
    remote_path: str | None = None


@dataclass(slots=True)
class DownloadReport:
    files: list[FileResult]
    failures: list[HostFailure]

    @property
    def ok(self) -> int:
        return len(self.files)

    @property
    def ng(self) -> int:
        return len(self.failures)

    @property
    def failure_ratio(self) -> float:
        """Fraction of attempted units that failed, for threshold-based
        alerting. 0.0 when nothing was attempted."""
        total = self.ok + self.ng
        return self.ng / total if total else 0.0

    def grouped(self) -> dict[str, dict[str, bytes]]:
        """Collected files as one nested dict: ``{host: {remote_path: data}}``.

        Convenience for the "process everything after it's all in memory"
        workflow — iterate this single structure, parse/transform each file's
        bytes, then ship to OpenSearch. Empty when ``download`` ran with an
        ``on_file`` callback (the bytes were streamed out, not retained)."""
        out: dict[str, dict[str, bytes]] = {}
        for f in self.files:
            out.setdefault(f.host, {})[f.remote_path] = f.data
        return out


@dataclass(slots=True)
class UploadReport:
    """Outcome of a fleet upload run, mirroring ``DownloadReport``'s shape so
    the same ``ok`` / ``ng`` / ``failure_ratio`` threshold-alerting code works
    unchanged for the write direction."""

    results: list[UploadResult]
    failures: list[HostFailure]

    @property
    def ok(self) -> int:
        return len(self.results)

    @property
    def ng(self) -> int:
        return len(self.failures)

    @property
    def failure_ratio(self) -> float:
        """Fraction of attempted units that failed, for threshold-based
        alerting. 0.0 when nothing was attempted."""
        total = self.ok + self.ng
        return self.ng / total if total else 0.0

    def grouped(self) -> dict[str, list[str]]:
        """Uploaded paths as ``{host: [remote_path, ...]}``."""
        out: dict[str, list[str]] = {}
        for r in self.results:
            out.setdefault(r.host, []).append(r.remote_path)
        return out


@dataclass(slots=True)
class HostListing:
    """The remote paths discovered on one host. ``paths`` may be empty if the
    host connected but its directories were empty or all listings failed (the
    failures are recorded separately on the report)."""

    host: str
    paths: list[str]


@dataclass(slots=True)
class ListingReport:
    """Result of listing the fleet's directories without fetching anything.

    This is the "look before you download" step for a large fleet: list the
    measurement dirs across all ~300 hosts, decide what's worth pulling, then
    feed the chosen paths back into ``download`` via ``to_specs()``.
    """

    listings: list[HostListing]
    failures: list[HostFailure]

    @property
    def ok(self) -> int:
        return len(self.listings)

    @property
    def ng(self) -> int:
        return len(self.failures)

    @property
    def total_paths(self) -> int:
        return sum(len(l.paths) for l in self.listings)

    def grouped(self) -> dict[str, list[str]]:
        """Discovered paths as ``{host: [remote_path, ...]}``."""
        return {l.host: l.paths for l in self.listings}

    def to_specs(self) -> list["HostSpec"]:
        """Turn discovered paths into download-ready ``HostSpec`` objects.

        Each host's paths become fixed ``files`` (no re-listing on download).
        Hosts that discovered nothing are dropped. This closes the loop:
        ``downloader.download(report.to_specs())``.
        """
        return [
            HostSpec(host=l.host, files=list(l.paths)) for l in self.listings if l.paths
        ]


@runtime_checkable
class FleetTransport(Protocol):
    """The interchange seam between the two FTP deployment paths.

    Two adapters satisfy it: the direct ``FtpFleetDownloader`` (this module,
    real FTP) and the HTTP-proxy ``FtpFleetDownloader`` (``ftp_flask_downloader``,
    same surface over HTTP). A call site swaps one import line between them and
    nothing else changes — that swap is the whole point of the seam.

    All three fleet operations are on the seam: ``list_dirs`` (the
    look-before-you-download listing pass), ``download``, and ``upload`` (the
    write direction). The conformance test guards that both adapters keep
    matching every method, so neither path drifts.
    """

    def download(
        self, specs: list[HostSpec], *, on_file: OnFile | None = None
    ) -> DownloadReport: ...

    def list_dirs(self, specs: list[HostSpec]) -> ListingReport: ...

    def upload(self, specs: list[UploadSpec]) -> UploadReport: ...


class FtpFleetDownloader:
    """Reusable, synchronous, concurrent FTP downloader for a host fleet.

    Construct once with shared credentials and tuning, then call ``download``
    (sync) as many times as you like::

        dl = FtpFleetDownloader(user="ftpuser", password="ftppass")
        report = dl.download([
            HostSpec("10.0.0.1", files=["/HITACHI/SYSFILE/LOG_RECIPE_EXE.log"]),
            HostSpec("10.0.0.2", listings=[ListDir("/MEAS", "*.dat")]),
        ])
        print(report.ok, report.ng)
        for f in report.files:
            handle(f.host, f.remote_path, f.data)
    """

    def __init__(
        self,
        *,
        user: str,
        password: str,
        port: int = 21,
        max_concurrency: int = 48,
        connect_timeout: float = 8.0,
        host_timeout: float = 60.0,
        passive: bool = True,
    ) -> None:
        # connect_timeout is the connection-wait knob: it bounds each blocking
        # socket op (connect, login, RETR). 8s means an offline/black-holed tool
        # is abandoned in 8s instead of hanging a slot — essential at hundreds
        # of hosts. host_timeout backstops a whole host (connect + listing + all
        # its files); it does NOT govern dead-host detection (connect_timeout
        # does), so keep it comfortably above connect_timeout x files-per-host.
        # It only fires for a host that connects then stalls mid-transfer.
        # passive=True is ftplib's default and the right choice behind NAT, but
        # is exposed because a worker on a different subnet than your laptop can
        # need the opposite — the classic second "works locally, not on the
        # server" gotcha after concurrency.
        self.user = user
        self.password = password
        self.port = port
        self.max_concurrency = max_concurrency
        self.connect_timeout = connect_timeout
        self.host_timeout = host_timeout
        self.passive = passive

    # ── public sync API ─────────────────────────────────────────────────────
    def download(
        self,
        specs: list[HostSpec],
        *,
        on_file: OnFile | None = None,
    ) -> DownloadReport:
        """Download every spec concurrently and return a report.

        Synchronous and event-loop-free — the fan-out runs on a plain thread
        pool, so this is safe to call from anywhere: a script, an Airflow task,
        a Flask request handler, or a scheduler thread, including one that
        already runs its own asyncio loop. Pass ``on_file`` to stream-process
        each file and keep RAM bounded; omit it to collect all bytes into the
        report.
        """
        files, failures = self._run_fleet(
            specs, lambda spec: self._host_worker(spec, on_file)
        )
        return DownloadReport(files=files, failures=failures)

    def list_dirs(self, specs: list[HostSpec]) -> ListingReport:
        """List each host's ``listings`` directories concurrently — no fetching.

        The "look before you download" pass for a large fleet: enumerate the
        measurement dirs across all hosts, inspect ``report.grouped()`` /
        ``report.total_paths`` to decide what's worth pulling, then download the
        survivors with ``downloader.download(report.to_specs())``. Only
        ``spec.listings`` is consulted; ``spec.files`` is ignored (you already
        know those paths — list to *discover* unknown ones).

        Same concurrency, timeout, per-host failure isolation, and
        event-loop-free safety as ``download``.
        """
        listings, failures = self._run_fleet(specs, self._list_worker)
        return ListingReport(listings=listings, failures=failures)

    def upload(self, specs: list[UploadSpec]) -> UploadReport:
        """Push every spec's files to its host concurrently and return a report.

        The write-direction counterpart to ``download``: each host's files are
        STOR'd over one reused connection, overwriting any file already at the
        destination path. Same concurrency cap, per-host ``host_timeout``
        backstop, per-host AND per-file failure isolation, and event-loop-free
        safety as ``download`` — they share the ``_run_fleet`` engine.

        Bytes live in memory here (``UploadSpec.files`` carries them), so peak
        RAM is the sum of all queued upload data; for a large push, send it in
        chunks of specs rather than one giant call.
        """
        results, failures = self._run_fleet(specs, self._upload_worker)
        return UploadReport(results=results, failures=failures)

    # ── concurrent orchestration (private) ──────────────────────────────────
    @contextmanager
    def _session(self, host: str) -> Iterator[FTP]:
        """One connected, logged-in FTP session for ``host``, closed on exit.

        Shared open/login/passive setup for both the download and listing
        workers, so a host is always reached the same way.
        """
        with FTP(timeout=self.connect_timeout) as ftp:
            ftp.connect(host=host, port=self.port, timeout=self.connect_timeout)
            ftp.login(user=self.user, passwd=self.password)
            ftp.set_pasv(self.passive)
            yield ftp

    def _run_fleet(
        self,
        specs: list[HostSpec],
        worker: "Callable[[HostSpec], tuple[list, list[HostFailure]]]",
    ) -> tuple[list, list[HostFailure]]:
        """Fan ``worker`` out across ``specs`` on a thread pool and aggregate.

        The shared engine behind ``download`` and ``list_dirs``. A
        ``ThreadPoolExecutor`` sized to ``max_concurrency`` caps simultaneous
        connections; every host is backstopped by ``host_timeout``; a raise from
        one host never aborts its siblings (partial success is the normal case).
        ``worker`` runs blocking in a pool thread and returns
        ``(ok_items, failures)``; what's in ``ok_items`` is the caller's business
        (``FileResult`` or ``HostListing``).

        ``host_timeout`` is measured from when each host's worker *starts*
        running, not from submit — so a host queued behind a full pool isn't
        charged for its wait, and a host that has started can't gain extra budget
        by finishing while we happen to be blocked on an earlier future.

        No asyncio event loop is involved, so this is safe even when called from
        inside an already-running loop (e.g. an async web worker).
        """
        # Pool sized to max_concurrency so at most that many connections are
        # open at once. shutdown(wait=False): a host that connects then stalls
        # mid-transfer can't be force-cancelled — we abandon its result after
        # host_timeout rather than block teardown, and connect_timeout bounds
        # each socket op so the abandoned thread drains on its own shortly after.
        pool = ThreadPoolExecutor(
            max_workers=self.max_concurrency, thread_name_prefix="ftp-fleet"
        )
        started: dict[int, float] = {}
        started_lock = threading.Lock()

        def _timed(idx: int, spec: HostSpec):
            # Stamp the start time before any blocking work so host_timeout is
            # measured from here, not from when the future was submitted.
            with started_lock:
                started[idx] = time.monotonic()
            return worker(spec)

        ok: list = []
        failures: list[HostFailure] = []
        try:
            futures = [
                (pool.submit(_timed, idx, spec), idx, spec)
                for idx, spec in enumerate(specs)
            ]
            # Iterate in submission order so failures stay in spec order.
            for future, idx, spec in futures:
                # Wait for this host's worker to actually begin (it may be queued
                # behind a full pool), then bound it by what remains of its budget.
                while True:
                    with started_lock:
                        start = started.get(idx)
                    if start is not None or future.done():
                        break
                    time.sleep(0.01)
                remaining = (
                    None
                    if start is None
                    else max(0.0, self.host_timeout - (time.monotonic() - start))
                )
                try:
                    host_ok, host_failures = future.result(timeout=remaining)
                except FutureTimeoutError:
                    failures.append(
                        HostFailure(
                            host=spec.host,
                            error=f"TimeoutError: exceeded host_timeout={self.host_timeout}s",
                        )
                    )
                except Exception as exc:  # noqa: BLE001 - one host never sinks the fleet
                    failures.append(
                        HostFailure(
                            host=spec.host, error=f"{type(exc).__name__}: {exc}"
                        )
                    )
                else:
                    ok.extend(host_ok)
                    failures.extend(host_failures)
        finally:
            pool.shutdown(wait=False)
        return ok, failures

    # ── blocking per-host work (runs in a thread) ───────────────────────────
    def _host_worker(
        self,
        spec: HostSpec,
        on_file: OnFile | None,
    ) -> tuple[list[FileResult], list[HostFailure]]:
        files: list[FileResult] = []
        failures: list[HostFailure] = []
        try:
            with self._session(spec.host) as ftp:
                for remote_path in self._resolve_paths(ftp, spec, failures):
                    self._fetch_one(ftp, spec.host, remote_path, on_file, files, failures)
        except all_errors as exc:
            # connect / login / quit failed — no file got a chance.
            failures.append(
                HostFailure(host=spec.host, error=f"{type(exc).__name__}: {exc}")
            )
        return files, failures

    def _list_worker(
        self,
        spec: HostSpec,
    ) -> tuple[list[HostListing], list[HostFailure]]:
        # Discovery-only counterpart to _host_worker: connect once, enumerate
        # each listing dir, never RETR. A host that connects always yields a
        # HostListing (possibly empty); a listing that fails to enumerate is
        # recorded but doesn't sink the host's other listings.
        paths: list[str] = []
        failures: list[HostFailure] = []
        try:
            with self._session(spec.host) as ftp:
                for listing in spec.listings:
                    try:
                        names = ftp.nlst(listing.remote_dir)
                    except all_errors as exc:
                        failures.append(
                            HostFailure(
                                host=spec.host,
                                error=f"list {listing.remote_dir} failed: {type(exc).__name__}: {exc}",
                                remote_path=listing.remote_dir,
                            )
                        )
                        continue
                    paths.extend(
                        _normalize_listing(names, listing.remote_dir, listing.pattern)
                    )
        except all_errors as exc:
            # connect / login failed — host discovered nothing.
            failures.append(
                HostFailure(host=spec.host, error=f"{type(exc).__name__}: {exc}")
            )
            return [], failures
        return [HostListing(host=spec.host, paths=paths)], failures

    def _upload_worker(
        self,
        spec: "UploadSpec",
    ) -> tuple[list[UploadResult], list[HostFailure]]:
        # Write-direction counterpart to _host_worker: connect once, STOR each
        # file. A connect/login failure sinks the whole host (no file got a
        # chance); a single STOR failure is isolated to that file and never
        # aborts the host's remaining uploads.
        results: list[UploadResult] = []
        failures: list[HostFailure] = []
        try:
            with self._session(spec.host) as ftp:
                for item in spec.files:
                    try:
                        ftp.storbinary(f"STOR {item.remote_path}", BytesIO(item.data))
                    except all_errors as exc:
                        failures.append(
                            HostFailure(
                                host=spec.host,
                                error=f"{type(exc).__name__}: {exc}",
                                remote_path=item.remote_path,
                            )
                        )
                    else:
                        results.append(
                            UploadResult(host=spec.host, remote_path=item.remote_path)
                        )
        except all_errors as exc:
            # connect / login failed — no file got a chance.
            failures.append(
                HostFailure(host=spec.host, error=f"{type(exc).__name__}: {exc}")
            )
        return results, failures

    def _resolve_paths(
        self,
        ftp: FTP,
        spec: HostSpec,
        failures: list[HostFailure],
    ) -> list[str]:
        # Fixed paths first, then expand each listing. A listing that fails to
        # enumerate is recorded but doesn't sink the fixed-path fetches.
        paths = list(spec.files)
        for listing in spec.listings:
            try:
                names = ftp.nlst(listing.remote_dir)
            except all_errors as exc:
                failures.append(
                    HostFailure(
                        host=spec.host,
                        error=f"list {listing.remote_dir} failed: {type(exc).__name__}: {exc}",
                        remote_path=listing.remote_dir,
                    )
                )
                continue
            paths.extend(_normalize_listing(names, listing.remote_dir, listing.pattern))
        return paths

    def _fetch_one(
        self,
        ftp: FTP,
        host: str,
        remote_path: str,
        on_file: OnFile | None,
        files: list[FileResult],
        failures: list[HostFailure],
    ) -> None:
        # Broad except: covers ftplib errors AND anything an on_file callback
        # raises (e.g. a MinIO/OpenSearch write), so a per-file failure is
        # isolated to that file and reported, never propagated.
        try:
            buf = BytesIO()
            ftp.retrbinary(f"RETR {remote_path}", buf.write)
            data = buf.getvalue()
            if on_file is not None:
                on_file(host, remote_path, data)
                # Drop the bytes once consumed — streaming mode keeps RAM flat.
                files.append(FileResult(host=host, remote_path=remote_path, data=b""))
            else:
                files.append(FileResult(host=host, remote_path=remote_path, data=data))
        except Exception as exc:  # noqa: BLE001 - intentional per-file isolation
            failures.append(
                HostFailure(
                    host=host,
                    error=f"{type(exc).__name__}: {exc}",
                    remote_path=remote_path,
                )
            )


def specs_from_hosts(
    hosts: list[str],
    *,
    files: list[str] | None = None,
    listings: list[ListDir] | None = None,
) -> list[HostSpec]:
    """Wrap a plain list of host IPs into ``HostSpec`` objects.

    The common case: every host shares the same fixed ``files`` and/or directory
    ``listings``. Each spec gets its own copy of the lists, so mutating one
    host's spec never bleeds into another's::

        specs = specs_from_hosts(ips, listings=[ListDir("/MEAS", "*.dat")])
        report = FtpFleetDownloader(user=u, password=p).list_dirs(specs)

    For per-host configuration that differs, build from JSON with
    ``eqp_ftp_collect.build_host_specs`` instead.
    """
    return [
        HostSpec(host=host, files=list(files or []), listings=list(listings or []))
        for host in hosts
    ]


def upload_specs_from_hosts(
    hosts: list[str],
    *,
    files: list[UploadFile],
) -> list[UploadSpec]:
    """Wrap a plain host list into ``UploadSpec`` objects sharing the same files.

    The common write case: push the SAME file(s) to every host (a recipe, a
    config drop). Each spec gets its own copy of the list so mutating one host's
    spec never bleeds into another's::

        specs = upload_specs_from_hosts(ips, files=[UploadFile("/INBOX/r.csv", data)])
        report = FtpFleetDownloader(user=u, password=p).upload(specs)
    """
    return [UploadSpec(host=host, files=list(files)) for host in hosts]


def upload_fleet(
    specs: list[UploadSpec],
    *,
    user: str,
    password: str,
    **kwargs: object,
) -> UploadReport:
    """One-call convenience wrapper around ``FtpFleetDownloader.upload``.

    ``upload_fleet(specs, user=..., password=...)`` pushes files across the
    fleet; extra keyword args (port, max_concurrency, connect_timeout,
    host_timeout, passive) are forwarded to the constructor.
    """
    downloader = FtpFleetDownloader(user=user, password=password, **kwargs)  # type: ignore[arg-type]
    return downloader.upload(specs)


def download_fleet(
    specs: list[HostSpec],
    *,
    user: str,
    password: str,
    on_file: OnFile | None = None,
    **kwargs: object,
) -> DownloadReport:
    """One-call convenience wrapper around ``FtpFleetDownloader``.

    For callers that just want a function: ``download_fleet(specs, user=...,
    password=...)``. Extra keyword args (port, max_concurrency, connect_timeout,
    host_timeout, passive) are forwarded to the constructor.
    """
    downloader = FtpFleetDownloader(user=user, password=password, **kwargs)  # type: ignore[arg-type]
    return downloader.download(specs, on_file=on_file)


def list_fleet(
    specs: list[HostSpec],
    *,
    user: str,
    password: str,
    **kwargs: object,
) -> ListingReport:
    """One-call convenience wrapper for the fleet-wide listing pass.

    ``list_fleet(specs, user=..., password=...)`` discovers paths across the
    fleet; extra keyword args (port, max_concurrency, connect_timeout,
    host_timeout, passive) are forwarded to the constructor.
    """
    downloader = FtpFleetDownloader(user=user, password=password, **kwargs)  # type: ignore[arg-type]
    return downloader.list_dirs(specs)


# Characters illegal in a Windows path component (plus control chars). A Linux
# FTP server can produce filenames containing these; they'd crash a write on a
# Windows client, so each path component is sanitized before landing on disk.
_ILLEGAL_COMPONENT = re.compile(r'[<>:"|?*\x00-\x1f]')


def _safe_relative(remote_path: str) -> Path:
    """Map an FTP remote path to a safe RELATIVE local Path.

    Handles both POSIX and Windows-FTP separators, strips path-traversal and
    drive components, and sanitizes characters illegal on the local FS. The
    remote directory structure is otherwise preserved.
    """
    # A Windows-hosted FTP server may use backslashes; normalize to one form.
    normalized = remote_path.replace("\\", "/")
    parts: list[str] = []
    for raw in normalized.split("/"):
        if raw in ("", ".", ".."):
            continue  # drop leading slash, current- and parent-dir segments
        cleaned = _ILLEGAL_COMPONENT.sub("_", raw).rstrip(". ")  # Windows trims these
        if cleaned:
            parts.append(cleaned)
    return Path(*parts) if parts else Path("_unnamed")


def save_to_dir(dest_dir: str | Path, *, then: OnFile | None = None) -> OnFile:
    """Build an ``on_file`` callback that writes each file to local disk.

    Lands files at ``dest_dir/<host>/<remote path>``, creating parent dirs.
    Works in both direct and proxy mode — the write happens wherever the
    callback runs (on the client PC in proxy mode). Because it runs per file,
    RAM stays bounded (streaming), unlike collecting then writing.

    ``then`` chains a second callback after the write (e.g. parse + index), so
    you can archive to disk AND process in one pass.

        dl.download(specs, on_file=save_to_dir(r"C:\\eqp_downloads"))
    """
    base = Path(dest_dir)

    def on_file(host: str, remote_path: str, data: bytes) -> None:
        target = base / _ILLEGAL_COMPONENT.sub("_", host) / _safe_relative(remote_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        if then is not None:
            then(host, remote_path, data)

    return on_file
