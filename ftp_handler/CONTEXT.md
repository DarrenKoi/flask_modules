# Context — equipment FTP collection

Glossary for the FTP fleet downloader and its two deployment paths. Terms only;
no implementation detail (that lives in `ftp_fleet_downloader.md`).

## Terms

### Fleet
The full set of equipment FTP servers polled in one scheduled run (~200+).

### Host
One equipment FTP server. Modeled as exactly one `HostSpec`. All files pulled
from a host in a run travel over **one** FTP connection, opened once and reused
for the directory listing and every file fetch, then closed. One host = one
spec = one connection. Pulling many files from a host needs no separate
function — they are listed on that host's single spec and fetched sequentially.

### Direct path
Deployment where the code doing FTP runs somewhere that can reach the equipment
servers directly (the Airflow worker). Uses streaming mode, so peak memory is
bounded by `concurrency × file size`, not total fleet size.

### Proxy path
Deployment for a firewalled client that cannot reach the equipment servers. The
client POSTs specs over HTTP to a Flask proxy on a firewall-free host; the proxy
does the real FTP and returns file bytes. Both paths are in use.

### File-size class
Whether a run's files are "small" (KB–few MB) or "large" (~10MB+). Drives the
memory and timeout safety analysis differently per path — the direct path
tolerates large files easily; the proxy path is sensitive to them.
