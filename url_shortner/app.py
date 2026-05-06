"""Flask app factory exposing the shortener as an HTTP service."""

import os
from typing import Any

from flask import Flask, redirect, render_template, request

from .analytics import ClickAnalytics
from .cache import CacheLayer
from .mapping import URLMapping
from .service import AliasTakenError, ShortenerService


def _click_meta(req: Any) -> dict[str, Any]:
    return {
        "ip": req.headers.get("X-Forwarded-For", req.remote_addr),
        "user_agent": req.headers.get("User-Agent"),
        "referrer": req.referrer,
    }


def create_app(
    *,
    service: ShortenerService | None = None,
    base_url: str | None = None,
) -> Flask:
    """Build a Flask app wired to the given (or default) shortener service."""

    if service is None:
        service = ShortenerService(
            mapping=URLMapping(),
            cache=CacheLayer(),
            analytics=ClickAnalytics(),
        )

    resolved_base_url = base_url if base_url is not None else os.getenv(
        "URLSHORTNER_BASE_URL", ""
    )

    app = Flask(__name__)
    app.config["SHORTENER_SERVICE"] = service
    app.config["BASE_URL"] = resolved_base_url

    @app.get("/")
    def index() -> str:
        return render_template("index.html", base_url=resolved_base_url)

    @app.post("/shorten")
    def shorten() -> tuple[dict[str, Any], int]:
        body = request.get_json(force=True, silent=True) or {}
        url = body.get("url")
        if not url:
            return {"error": "url is required"}, 400

        try:
            code = service.shorten(
                url,
                alias=body.get("alias"),
                owner=body.get("owner"),
            )
        except ValueError as exc:
            return {"error": str(exc)}, 400
        except AliasTakenError as exc:
            return {"error": f"alias '{exc.args[0]}' is already in use"}, 409

        short_url = f"{resolved_base_url.rstrip('/')}/{code}" if resolved_base_url else code
        return {"code": code, "short_url": short_url}, 201

    @app.get("/<code>")
    def follow(code: str):
        url = service.resolve(code)
        if url is None:
            return {"error": "not found"}, 404
        service.record_click(code, _click_meta(request))
        return redirect(url, code=302)

    return app
