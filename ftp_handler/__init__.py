"""Equipment FTP fleet collection.

Self-contained package: the concurrent in-memory FTP downloader, the
archive→parse→index glue, and the firewalled-client Flask proxy pair, plus the
reference docs, CONTEXT glossary, and ADRs alongside them.

Import submodules directly (no re-export hub) so a worker that lacks ``flask`` /
``requests`` can still import the core downloader and glue::

    from ftp_handler.ftp_fleet_downloader import FtpFleetDownloader, HostSpec
    from ftp_handler.eqp_ftp_collect import build_host_specs, collect_fleet

The proxy pair (``ftp_flask_proxy`` / ``ftp_flask_downloader``) is also designed
to be copied out and run standalone on a client PC — it imports its sibling by
bare name, not through this package.
"""
