# ftp_handler — usage by case

Pick the subpackage that matches how you reach the FTP servers and at what
scale. Each subpackage is a re-export hub, so you import the leaf name. Runnable
versions of everything below live in each folder's `examples.py`.

| Case | You have… | Use | Import from |
|------|-----------|-----|-------------|
| 1. One server, ad-hoc | a single host, interactive/script | `FtpClient` | `ftp_handler.core` |
| 2. Many servers, direct | a process that can reach the FTP fleet | `FtpFleetDownloader` | `ftp_handler.direct_downloader` |
| 3. Many servers, firewalled | a PC that can't reach FTP but can reach a proxy | `FtpFleetDownloader` (HTTP) | `ftp_handler.proxy` |
| 4. Web app, non-blocking | an HTTP request must start a fleet run | `BackgroundJobs` | `ftp_handler.web_app` |
| 5. Airflow / scheduled | a periodic job | `collect_fleet` (direct) | `ftp_handler.direct_downloader` |

The seam: cases 2 and 3 expose the **same names**, so swapping direct ↔ proxy is
one import line and nothing else changes.

---

## Case 1 — single server (`core.FtpClient`)

One reused connection, used as a context manager. Four ad-hoc ops; server errors
propagate (no fleet report to absorb them).

```python
from ftp_handler.core import FtpClient

with FtpClient(host="10.0.0.1", user="u", password="p") as ftp:
    names = ftp.list_dir("/MEAS", pattern="*.dat")     # NLST (names)
    entries = ftp.list_entries("/MEAS")                # MLSD (dirs vs files)
    details = ftp.list_details("/MEAS")                # LIST (size + mtime, KST)
    data = ftp.download(names[0])                      # RETR -> bytes
    ftp.upload("/INBOX/report.csv", b"a,b\n")          # STOR
    ftp.remove("/MEAS/stale.dat")                      # DELE
```

Listing flavors: `list_dir` works everywhere; `list_entries` needs MLSD (RFC
3659) and raises `error_perm` on old daemons — fall back to `list_details`,
which parses Unix `ls -l` / MS-DOS text and returns typed `FileInfo`.

---

## Case 2 — concurrent fleet, direct FTP (`direct_downloader.FtpFleetDownloader`)

For a process that can reach the servers (a firewall-free host, an Airflow
worker). Synchronous and event-loop-free — safe from a script, a thread, or an
async context. Two phases:

```python
from ftp_handler.direct_downloader import FtpFleetDownloader, HostSpec, ListDir, specs_from_hosts

dl = FtpFleetDownloader(user="u", password="p", max_concurrency=48)

# Fixed known paths (no listing):
report = dl.download([HostSpec("10.0.0.1", files=["/HITACHI/SYSFILE/LOG.log"])])

# Discover first, then download (the "look before you download" pass):
discover = specs_from_hosts(hosts, listings=[ListDir("/MEAS", "*.dat")])
listing = dl.list_dirs(discover)            # no fetching
report = dl.download(listing.to_specs())    # fetch the chosen paths

print(report.ok, report.ng, report.failure_ratio)
```

**Bounded RAM:** pass `on_file` to stream each file out as it lands instead of
collecting the whole fleet in memory:

```python
from ftp_handler.direct_downloader import save_to_dir
dl.download(specs, on_file=save_to_dir("/data/eqp"))   # peak RAM ~ concurrency x file size
```

**Tuning:** `connect_timeout` abandons a dead host fast; `host_timeout` backstops
one that connects then stalls; `max_concurrency` caps connections (and RAM).
`download_fleet` / `list_fleet` are one-call function wrappers.

---

## Case 3 — firewalled client via the HTTP proxy (`proxy`)

Your PC can't reach the FTP servers but can reach a proxy on a firewall-free
host. The proxy does the real FTP; the client gets bytes over HTTP.

```
client PC ──HTTP──> Flask proxy ──FTP──> equipment servers
(firewalled)        (firewall-free)
```

**Server half** (on the firewall-free host) — mount the blueprint, or run
standalone; set `FTP_PROXY_TOKEN` and serve behind HTTPS:

```python
from ftp_handler.proxy.flask_proxy import ftp_proxy_sknn_v3   # or create_app()
app.register_blueprint(ftp_proxy_sknn_v3)
```

**Client half** — identical to the direct downloader, only the import differs:

```python
from ftp_handler.proxy import FtpFleetDownloader, HostSpec, ListDir, save_to_dir

dl = FtpFleetDownloader(
    user="u", password="p",
    proxy_url="https://proxy.host:8080", token="secret",   # or env FTP_PROXY_URL/TOKEN
)
report = dl.download(specs, on_file=save_to_dir(r"C:\eqp"))  # on_file runs locally
```

The dataclasses (`HostSpec`, `DownloadReport`, …) are the same objects as the
direct downloader's, so `report.grouped()`, `to_specs()`, etc. behave
identically. Copy-out: the pair plus `fleet_downloader.py` and `listing.py` can
be dropped flat on a client PC and imported by bare name.

---

## Case 4 — non-blocking run in a web server (`web_app.BackgroundJobs`)

`download()` blocks for the whole run, so don't call it inline in a request.
`BackgroundJobs` runs it on a background thread and returns a job id at once;
poll for the result.

```python
from ftp_handler.web_app import BackgroundJobs, create_jobs_blueprint
from ftp_handler.direct_downloader import FtpFleetDownloader, build_host_specs, save_to_dir

jobs = BackgroundJobs()                       # one fleet run at a time (max_workers=1)

def start(body: dict) -> str:                 # your app builds specs/creds
    specs = build_host_specs(body["fleet"])
    dl = FtpFleetDownloader(user="u", password="p")
    return jobs.submit(lambda: dl.download(specs, on_file=save_to_dir(body["dest"])))

app.register_blueprint(create_jobs_blueprint(jobs, start=start))
# POST /fleet/jobs {fleet,dest} -> 202 {"job_id"}
# GET  /fleet/jobs/<id>         -> 200 {status, result-counts, error}
```

**Scope:** the registry is in-process. Under `gunicorn -w N` a status poll may
hit a worker that never saw the job — run the collector in one dedicated process
(or back the registry with Redis). Status responses carry counts only, never
file bytes.

**Don't need it for purely scheduled runs:** an APScheduler job already runs off
the request thread — just call `dl.download(specs)` from the scheduled function.

---

## Case 5 — Airflow / scheduled collection (`direct_downloader.collect_fleet`)

`collect_fleet` streams each file through archive → parse → index, all callables
you supply, so it imports no `minio` / `opensearch` itself (the DAG injects the
concrete clients). See `airflow_mgmt/dags/eqp_ftp/eqp_ftp_collector_dag.py`.

```python
from ftp_handler.direct_downloader import collect_fleet, build_host_specs

specs = build_host_specs(fleet_json)          # from an Airflow Variable
report = collect_fleet(
    specs, user=u, password=p,
    archive=lambda h, p_, d: storage.put(f"{h}{p_}", d) or f"{h}{p_}",  # raw -> MinIO key
    parse=parse_records,                                                 # bytes -> docs
    index=lambda docs: doc.bulk_index("eqp_meas", docs),                 # docs -> OpenSearch
)
```

**Prerequisite:** the OpenSearch step needs `opensearch-py` available on the
worker (it's imported lazily inside `ops_store`), in addition to the repo being
importable. If it isn't installed, the task fails at runtime on `OSDoc()`.
