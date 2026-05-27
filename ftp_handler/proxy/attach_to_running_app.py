"""Copy-paste guide: mount the FTP proxy onto an already-running Flask app.

This is the SERVER half, no auth (single trusted user — FTP_PROXY_TOKEN unset,
so every request passes). Run this on a host that CAN reach the equipment FTP
servers. Registering the blueprint adds three routes to your existing app:

    POST /download_sknn_v3     # fan out a fleet download, return file bytes
    POST /list_dirs_sknn_v3    # discovery pass, return matching paths only
    GET  /healthz_sknn_v3      # {"status": "ok"}

The `_sknn_v3` suffix keeps these from colliding with paths your app already
serves, so there is nothing to rename on your side.

Pick the snippet that matches how your app is built and paste it in.
"""

from ftp_handler.proxy.flask_proxy import ftp_proxy_sknn_v3


# ── Case 1: you have an app factory (create_app) ──────────────────────────────
# Add the one register_blueprint line inside your factory, then return as before.
def create_app():
    from flask import Flask

    app = Flask(__name__)

    # ... your existing config and blueprints ...

    app.register_blueprint(ftp_proxy_sknn_v3)  # <-- the only line you add
    return app


# ── Case 2: you have a module-level `app = Flask(__name__)` ───────────────────
# Drop this single line next to where the app object is created:
#
#     app.register_blueprint(ftp_proxy_sknn_v3)


# ── Verify after restart ──────────────────────────────────────────────────────
# Once the server is back up, this should return {"status": "ok"}:
#
#     curl http://localhost:8000/healthz_sknn_v3
#
# host_timeout defaults to 45s (ADR 0001), chosen to fire before a 60s worker
# kill. If your WSGI worker timeout is shorter than 45s, raise it (gunicorn
# --timeout / uWSGI harakiri) or the worker will kill a fleet download midway.
