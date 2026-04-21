# Flask Logging Guide

## Purpose

This repository already has a root-level `logging_config.py` helper that is
meant to configure Flask application logging once at startup.

With the current setup, the recommended pattern is:

1. Configure `app.logger` once in `create_app()`
2. Use `current_app.logger` in routes and request-aware code
3. Pass a logger into deeper helper functions only when they need to log
4. Make log messages self-identifying so the source is easy to trace

This keeps logging simple and avoids repeated handler setup across modules.

## Current Logging Model

The current helper is built around:

- `configure_flask_logging(app, log_dir, log_name, ...)`
- one rotating file log such as `logs/flask/server.log`
- the Flask app logger, not one file per Python module

That means the logger itself does not automatically separate output by `.py`
file. To make issue tracing easier, log messages should include a stable prefix
that identifies the module and function.

Example prefixes:

- `api.routes.health_check`
- `api.routes.ping`
- `service.user.create`
- `service.search.run`

## Configure Logging Once

Configure logging once during app creation. Do not call the logging setup
helpers in every module.

```python
from flask import Flask, jsonify

from config import Config
from logging_config import configure_flask_logging
from .routes import api_bp


def create_app() -> Flask:
    app = Flask(__name__)
    app.config.from_object(Config)

    configure_flask_logging(
        app,
        log_dir="logs/flask",
        log_name="server",
        level="INFO",
    )

    app.register_blueprint(api_bp)

    @app.get("/")
    def index():
        app.logger.info("api.index called")
        return jsonify(
            {
                "service": "flask_modules",
                "message": "Flask server is running.",
                "api_base": "/api",
            }
        )

    return app
```

## Route Usage

In route modules, use `current_app.logger`. Do not import the Flask `app`
object directly into other modules just for logging.

```python
from flask import Blueprint, jsonify, current_app


api_bp = Blueprint("api", __name__, url_prefix="/api")


@api_bp.get("/health")
def health_check():
    current_app.logger.info("api.routes.health_check called")
    return jsonify({"status": "ok"})


@api_bp.get("/ping")
def ping():
    current_app.logger.info("api.routes.ping called")
    return jsonify({"message": "pong"})
```

## Helper Function Usage

If a lower-level helper needs to log, prefer passing a logger from the route or
service boundary instead of importing the app globally.

```python
from flask import current_app


def run_search(query: str, logger) -> dict:
    logger.info("service.search.run query=%s", query)
    return {"query": query, "status": "ok"}


def search_route() -> dict:
    return run_search("example", current_app.logger)
```

This keeps helper code reusable and avoids hidden Flask dependencies.

## Exception Logging

When handling exceptions, use `logger.exception(...)` so the traceback is
written to the log file.

```python
from flask import current_app


def create_user(payload: dict) -> dict:
    try:
        user_id = payload["user_id"]
        return {"user_id": user_id}
    except Exception:
        current_app.logger.exception(
            "service.user.create failed payload_keys=%s",
            sorted(payload.keys()),
        )
        raise
```

Use `exception()` inside an `except` block. Use `error()` when you only need an
error message without a traceback.

## Message Style

With the current logging setup, message style matters more than logger naming.
Use a message format that is easy to scan and search.

Recommended shape:

- stable source prefix
- short event name
- a few key fields
- no secrets or raw credentials

Examples:

```python
current_app.logger.info("api.routes.ping called")
current_app.logger.info("service.user.lookup user_id=%s", user_id)
current_app.logger.warning("service.search.empty_query query=%r", query)
current_app.logger.exception("service.user.create failed user_id=%s", user_id)
```

## What To Avoid

- Do not call `configure_flask_logging()` or `configure_logging()` in each file
- Do not import the global Flask `app` object into modules just for logging
- Do not log passwords, tokens, cookies, or authorization headers
- Do not log entire request bodies unless they are sanitized first
- Do not fill the log with low-signal messages for every small internal step

## Recommended Team Convention

For this repository, the most practical convention is:

1. Configure `configure_flask_logging(app, ...)` once in `create_app()`
2. Use `current_app.logger` in `api/` routes
3. Pass `current_app.logger` into lower-level helpers when logging is needed
4. Prefix each message with a stable module or function label
5. Use `logger.exception(...)` for failures that need tracebacks

This gives you one managed log file with clear, searchable source information
while keeping the current logger design unchanged.
