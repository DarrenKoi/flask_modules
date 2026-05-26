"""Worked examples for ftp_handler.proxy — the firewalled-client HTTP transport.

Two halves on two machines:
  - SERVER (this office host can reach the FTP servers): run flask_proxy.
  - CLIENT (your firewalled PC, can reach the proxy but not the FTP servers):
    use proxy.FtpFleetDownloader exactly like the direct one.

The client surface is identical to direct_downloader — the only difference is
the import line, so a call site swaps transports without any other change.
Not tests; a copy-paste reference.
"""

import os

# CLIENT side: same names as the direct downloader, over HTTP.
from ftp_handler.proxy import FtpFleetDownloader, HostSpec, ListDir, save_to_dir

USER = "ftpuser"
PASSWORD = "ftppass"
FLEET_HOSTS = ["10.0.0.1", "10.0.0.2", "10.0.0.3"]


def example_run_the_proxy_server() -> None:
    """SERVER half — run on a firewall-free host that can reach the FTP servers.

    Mount the blueprint on your existing Flask app, or run standalone. Set
    FTP_PROXY_TOKEN to require a bearer token; always serve behind HTTPS (the FTP
    password and file bytes cross this connection).

        from ftp_handler.proxy.flask_proxy import ftp_proxy_sknn_v3
        app.register_blueprint(ftp_proxy_sknn_v3)
    """
    from ftp_handler.proxy.flask_proxy import create_app

    os.environ.setdefault("FTP_PROXY_TOKEN", "change-me")
    create_app().run(host="0.0.0.0", port=8080)


def example_download_through_proxy() -> None:
    """CLIENT half — drop-in for the direct downloader, over HTTP.

    Point it at the proxy via proxy_url/token (or env FTP_PROXY_URL /
    FTP_PROXY_TOKEN). on_file still runs HERE on the client, so save_to_dir lands
    files on your local PC.
    """
    specs = [HostSpec(host, listings=[ListDir("/MEAS", "*.dat")]) for host in FLEET_HOSTS]
    dl = FtpFleetDownloader(
        user=USER,
        password=PASSWORD,
        proxy_url="https://proxy.host:8080",
        token="change-me",
    )
    report = dl.download(specs, on_file=save_to_dir(r"C:\eqp_downloads"))
    print(f"ok={report.ok} ng={report.ng}")


def example_swap_direct_for_proxy() -> None:
    """The whole point of the seam: one import line changes the transport.

        # direct (firewall-free host):
        from ftp_handler.direct_downloader import FtpFleetDownloader
        # via the proxy (firewalled client):
        from ftp_handler.proxy import FtpFleetDownloader

    Everything below — specs, download(), the report, on_file — is identical.
    """
    specs = [HostSpec(host, files=["/HITACHI/SYSFILE/LOG_RECIPE_EXE.log"]) for host in FLEET_HOSTS]
    report = FtpFleetDownloader(user=USER, password=PASSWORD).download(specs)
    print(report.grouped().keys())


if __name__ == "__main__":
    # example_run_the_proxy_server()      # on the proxy host
    # example_download_through_proxy()    # on the firewalled client
    pass
