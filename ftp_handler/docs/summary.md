# ftp_handler — purpose summary

Equipment FTP collection, organized into four purpose-based subpackages. Each is
a re-export hub, so call sites import the leaf name. The direct and proxy
downloaders share one public surface (the `FleetTransport` seam), so swapping
transports is a one-line import change. See `docs/usage.md` for per-case
recipes; each folder's `examples.py` has runnable code.

## Layout

```
ftp_handler/
  core/                 shared primitives (stdlib only; depends on nothing else)
    client.py           FtpClient — one server, ad-hoc list/download/upload/remove
    listing.py          _normalize_listing — NLST normalizer used at both scales
  direct_downloader/    talk to the FTP servers directly
    fleet_downloader.py FtpFleetDownloader — concurrent fan-out + list_dirs discovery
    collect.py          collect_fleet — archive → parse → index glue (no minio/opensearch imports)
  proxy/                firewalled-client HTTP transport
    flask_proxy.py      server half (needs flask) — does the real FTP, returns base64 bytes
    proxy_downloader.py client half (needs requests) — same surface as direct, over HTTP
  web_app/
    jobs.py             BackgroundJobs — run a fleet download off a web request thread
  docs/  + examples.py in each folder
```

## Subpackages

### `core` — shared primitives
- **`FtpClient`** (`client.py`): one reused connection as a context manager; four
  ad-hoc ops (`download`/`upload`/`remove`) plus three listing flavors —
  `list_dir` (NLST), `list_entries` (MLSD, splits dirs/files), `list_details`
  (LIST → typed `FileInfo` with size + KST-aware mtime). Server errors propagate.
- **`_normalize_listing`** (`listing.py`): the NLST path normalizer, shared so the
  single-server and fleet downloaders behave identically. Stdlib only, so it
  travels with the copy-out proxy bundle by bare name.

### `direct_downloader` — concurrent fleet, direct FTP
- **`FtpFleetDownloader`** (`fleet_downloader.py`): synchronous, **event-loop-free**
  (plain `ThreadPoolExecutor`), safe from any context including async web
  workers. `download` (collect or stream via `on_file`) and `list_dirs`
  (discovery pass → `to_specs()` → `download`) share one engine with
  `max_concurrency` cap, per-host timeout, and per-host failure isolation.
  Helpers: `specs_from_hosts`, `download_fleet`, `list_fleet`, `save_to_dir`.
- **`collect_fleet` / `build_host_specs`** (`collect.py`): archive → parse → index
  per file, steps injected as callables, so it imports no minio/opensearch — the
  DAG stays thin and this layer is unit-testable.

### `proxy` — firewalled-client HTTP transport
- **`flask_proxy.py`** (server): runs where FTP egress is allowed; a Flask
  blueprint (`/download_sknn_v3`, `/list_dirs_sknn_v3`, `/healthz_sknn_v3`),
  optional `FTP_PROXY_TOKEN`. Reuses `FtpFleetDownloader` for the real FTP.
- **`proxy_downloader.py`** (client): the package re-exports its
  `FtpFleetDownloader` — same names and same dataclasses as `direct_downloader`,
  over HTTP. Batches specs, POSTs concurrently; `on_file` runs locally. The
  `__init__` does NOT import `flask_proxy`, so importing the client never needs
  `flask`.

### `web_app` — non-blocking runs in a server
- **`BackgroundJobs`** (`jobs.py`): runs the blocking `download()` on a background
  thread; `submit()` returns a job id immediately, `get()` returns a snapshot for
  polling. `create_jobs_blueprint` exposes submit/status routes. In-process
  registry (single-process scope); status serialization carries counts, never
  file bytes.

## How the pieces fit

```
core.FtpClient ──────────── one server
core.listing ───── shared NLST normalizer ─────────────────────────┐
                                                                    │
direct_downloader.FtpFleetDownloader ── FleetTransport seam ── proxy.FtpFleetDownloader (HTTP)
        │  (list_dirs → to_specs → download)                        proxy.flask_proxy (real FTP)
direct_downloader.collect_fleet (archive→parse→index via on_file)
web_app.BackgroundJobs ── runs download() off the request thread
```

Tests patch `ftp_handler.core.client.FTP` and
`ftp_handler.direct_downloader.fleet_downloader.FTP` — never a live server. See
`docs/usage.md` for recipes and `docs/adr/` for decisions.
