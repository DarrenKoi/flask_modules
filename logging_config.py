"""Reusable logging helpers for Flask-oriented services."""

import logging
import os
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from typing import Any


DEFAULT_FORMAT = (
    "%(asctime)s %(levelname)s [pid=%(process)d thread=%(threadName)s] "
    "[%(name)s] %(message)s"
)
DEFAULT_RETENTION_DAYS = 3


def _resolve_level(level: int | str | None = None) -> int:
    if level is None:
        return logging.INFO

    if isinstance(level, int):
        return level

    resolved = getattr(logging, level.upper(), None)
    if isinstance(resolved, int):
        return resolved
    raise ValueError(f"Invalid logging level: {level!r}")


def _resolve_logger(
    logger: logging.Logger | None = None,
    logger_name: str | None = None,
) -> logging.Logger:
    if logger is not None and logger_name is not None:
        raise ValueError("Use either logger or logger_name, not both.")

    if logger is not None:
        return logger

    if logger_name is not None:
        return logging.getLogger(logger_name)

    return logging.getLogger()


def _resolve_log_path(log_dir: str | Path, log_name: str) -> Path:
    filename = log_name if log_name.endswith(".log") else f"{log_name}.log"
    return Path(os.path.abspath(Path(log_dir).expanduser() / filename))


def _has_rotating_handler(logger: logging.Logger, log_path: Path) -> bool:
    return any(
        isinstance(handler, TimedRotatingFileHandler)
        and Path(handler.baseFilename) == log_path
        for handler in logger.handlers
    )


def configure_logging(
    log_dir: str | Path,
    log_name: str,
    *,
    logger: logging.Logger | None = None,
    logger_name: str | None = None,
    level: int | str | None = None,
    fmt: str = DEFAULT_FORMAT,
    when: str = "midnight",
    interval: int = 1,
    backup_count: int = DEFAULT_RETENTION_DAYS,
    encoding: str = "utf-8",
    propagate: bool | None = None,
) -> logging.Logger:
    """Attach a timed rotating file handler to a logger."""

    target_logger = _resolve_logger(logger=logger, logger_name=logger_name)
    target_logger.setLevel(_resolve_level(level))

    if propagate is not None:
        target_logger.propagate = propagate

    log_path = _resolve_log_path(log_dir, log_name)

    if not _has_rotating_handler(target_logger, log_path):
        log_path.parent.mkdir(parents=True, exist_ok=True)
        handler = TimedRotatingFileHandler(
            log_path,
            when=when,
            interval=interval,
            backupCount=backup_count,
            encoding=encoding,
            delay=True,
        )
        handler.setFormatter(logging.Formatter(fmt))
        target_logger.addHandler(handler)

    return target_logger


def configure_flask_logging(
    app: Any,
    log_dir: str | Path,
    log_name: str,
    **kwargs: Any,
) -> logging.Logger:
    """Configure file logging for a Flask app logger."""

    return configure_logging(log_dir, log_name, logger=app.logger, **kwargs)


def setup_logger(
    path_dir: str | Path,
    name: str,
    *,
    level: int | str | None = None,
    fmt: str = DEFAULT_FORMAT,
    when: str = "midnight",
    interval: int = 1,
    backup_count: int = DEFAULT_RETENTION_DAYS,
    encoding: str = "utf-8",
    propagate: bool = False,
) -> logging.Logger:
    """Compatibility wrapper for older logging helpers.

    The older helper used the same `name` for both the logger name and the
    log file basename. Keep that behavior so existing call sites can move over
    without changing their arguments.
    """

    return configure_logging(
        path_dir,
        name,
        logger_name=name,
        level=level,
        fmt=fmt,
        when=when,
        interval=interval,
        backup_count=backup_count,
        encoding=encoding,
        propagate=propagate,
    )
