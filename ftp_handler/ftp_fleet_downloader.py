"""In-memory concurrent FTP fleet downloader.

Pulls files from many equipment FTP servers at once and hands the raw bytes
back to the caller. Async fan-out is an implementation detail: callers use the
plain synchronous ``download()`` method (or the ``download_fleet()`` helper) and
never touch a coroutine, so this drops into a non-async script or an Airflow
``PythonOperator`` unchanged.

Why threads, not aioftp:
  Each host is one short-lived FTP session that is almost entirely socket I/O,
  and Python releases the GIL during socket I/O. So ``asyncio.to_thread`` over
  blocking ``ftplib`` fans out N hosts concurrently with zero extra packages —
  no aioftp to pip-install into an Airflow venv, no version drift between your
  laptop and the worker. The event loop only orchestrates; the blocking
  ftplib calls run in a bounded thread pool.

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

import asyncio
import fnmatch
import re
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field
from ftplib import FTP, all_errors
from io import BytesIO
from pathlib import Path
from typing import Callable, Iterator, Protocol, runtime_checkable

# Invoked once per successfully downloaded file: (host, remote_path, data).
OnFile = Callable[[str, str, bytes], None]


def _normalize_listing(
    names: list[str],
    remote_dir: str,
    pattern: str | None = None,
) -> list[str]:
    """Filter and normalize raw NLST output into usable remote paths.

    NLST returns either bare basenames or full paths depending on the server;
    this normalizes both to a path that RETR/DELE will accept, and keeps only
    entries whose basename matches ``pattern`` (an fnmatch glob). ``pattern=None``
    keeps everything. Lives here (not in ``ftp_client``) so the fleet downloader
    stays free of any ``ftp_handler`` package dependency — it must import by bare
    name when copied out beside the Flask proxy pair. The single-server
    ``FtpClient`` imports it from here so listing behaves identically at both
    scales.
    """
    out: list[str] = []
    for name in names:
        base = name.rsplit("/", 1)[-1]
        if pattern is None or fnmatch.fnmatch(base, pattern):
            full = name if name.startswith("/") else f"{remote_dir.rstrip('/')}/{base}"
            out.append(full)
    return out


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

    Both phases of a fleet run are on the seam: ``list_dirs`` (the
    look-before-you-download listing pass) and ``download``. The conformance test
    guards that both adapters keep matching both methods, so neither path drifts.
    """

    def download(
        self, specs: list[HostSpec], *, on_file: OnFile | None = None
    ) -> DownloadReport: ...

    def list_dirs(self, specs: list[HostSpec]) -> ListingReport: ...


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

        Synchronous: spins up its own event loop internally. Do NOT call from
        already-async code — ``asyncio.run`` refuses to nest in a running loop.
        Pass ``on_file`` to stream-process each file and keep RAM bounded; omit
        it to collect all bytes into the report.
        """
        return asyncio.run(self._download_all(specs, on_file))

    def list_dirs(self, specs: list[HostSpec]) -> ListingReport:
        """List each host's ``listings`` directories concurrently — no fetching.

        The "look before you download" pass for a large fleet: enumerate the
        measurement dirs across all hosts, inspect ``report.grouped()`` /
        ``report.total_paths`` to decide what's worth pulling, then download the
        survivors with ``downloader.download(report.to_specs())``. Only
        ``spec.listings`` is consulted; ``spec.files`` is ignored (you already
        know those paths — list to *discover* unknown ones).

        Same concurrency, timeout, and per-host failure isolation as ``download``.
        Synchronous; do NOT call from already-async code.
        """
        return asyncio.run(self._list_all(specs))

    # ── async orchestration (private) ───────────────────────────────────────
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

    async def _run_fleet(
        self,
        specs: list[HostSpec],
        worker: "Callable[[HostSpec], tuple[list, list[HostFailure]]]",
    ) -> tuple[list, list[HostFailure]]:
        """Fan ``worker`` out across ``specs`` concurrently and aggregate.

        The shared engine behind ``download`` and ``list_dirs``: a pool + a
        semaphore cap simultaneous connections at ``max_concurrency``, every host
        is backstopped by ``host_timeout``, and a raise from one host never
        aborts its siblings (partial success is the normal case). ``worker`` runs
        blocking in a thread and returns ``(ok_items, failures)``; what's in
        ``ok_items`` is the caller's business (``FileResult`` or ``HostListing``).
        """
        loop = asyncio.get_running_loop()
        # A dedicated pool sized to max_concurrency — the default to_thread
        # executor caps at min(32, cpu+4), which would silently throttle a
        # higher max_concurrency. Semaphore + pool size match so at most
        # max_concurrency connections are open at once.
        pool = ThreadPoolExecutor(
            max_workers=self.max_concurrency, thread_name_prefix="ftp-fleet"
        )
        loop.set_default_executor(pool)
        sem = asyncio.Semaphore(self.max_concurrency)

        async def run_host(spec: HostSpec) -> tuple[list, list[HostFailure]]:
            async with sem:
                try:
                    return await asyncio.wait_for(
                        asyncio.to_thread(worker, spec),
                        timeout=self.host_timeout,
                    )
                except asyncio.TimeoutError:
                    # wait_for gives up waiting; the underlying thread can't be
                    # cancelled but ftplib's own timeout will end it shortly.
                    return [], [
                        HostFailure(
                            host=spec.host,
                            error=f"TimeoutError: exceeded host_timeout={self.host_timeout}s",
                        )
                    ]

        try:
            # return_exceptions=True so one unexpected error never aborts the
            # rest of the fleet — partial success is the normal case.
            outcomes = await asyncio.gather(
                *(run_host(s) for s in specs), return_exceptions=True
            )
        finally:
            pool.shutdown(wait=False)

        ok: list = []
        failures: list[HostFailure] = []
        for spec, outcome in zip(specs, outcomes):
            if isinstance(outcome, Exception):
                failures.append(
                    HostFailure(
                        host=spec.host, error=f"{type(outcome).__name__}: {outcome}"
                    )
                )
            else:
                host_ok, host_failures = outcome
                ok.extend(host_ok)
                failures.extend(host_failures)
        return ok, failures

    async def _download_all(
        self,
        specs: list[HostSpec],
        on_file: OnFile | None,
    ) -> DownloadReport:
        files, failures = await self._run_fleet(
            specs, lambda spec: self._host_worker(spec, on_file)
        )
        return DownloadReport(files=files, failures=failures)

    async def _list_all(self, specs: list[HostSpec]) -> ListingReport:
        listings, failures = await self._run_fleet(specs, self._list_worker)
        return ListingReport(listings=listings, failures=failures)

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
