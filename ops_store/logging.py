"""Logging helpers for the local OpenSearch package."""

import logging
import os
from pathlib import Path
from typing import Any


LOGGER_NAME = "opensearch"
DEFAULT_FORMAT = (
    "%(asctime)s %(levelname)s [pid=%(process)d thread=%(threadName)s] "
    "[%(name)s] %(message)s"
)
DEFAULT_LOG_DIR = Path(__file__).resolve().parent.parent / "logs" / "opensearch"


def _resolve_level(level: int | str | None = None) -> int:
    if level is None:
        level = os.getenv("OPENSEARCH_LOG_LEVEL", "INFO")

    if isinstance(level, int):
        return level

    resolved = getattr(logging, level.upper(), None)
    if isinstance(resolved, int):
        return resolved
    raise ValueError(f"Invalid logging level: {level!r}")


def get_logger(name: str | None = None) -> logging.Logger:
    if name:
        return logging.getLogger(f"{LOGGER_NAME}.{name}")
    return logging.getLogger(LOGGER_NAME)


class PIDFileHandler(logging.Handler):
    """File handler that resolves the log file from the current process ID."""

    def __init__(
        self,
        log_dir: str | Path,
        *,
        file_prefix: str = LOGGER_NAME,
        encoding: str = "utf-8",
        delay: bool = True,
    ) -> None:
        super().__init__()
        self.log_dir = Path(log_dir)
        self.file_prefix = file_prefix
        self.encoding = encoding
        self.delay = delay
        self._pid: int | None = None
        self._handler: logging.FileHandler | None = None

    @property
    def baseFilename(self) -> str:
        return str(self._build_path(os.getpid()))

    def _build_path(self, pid: int) -> Path:
        return self.log_dir / f"{self.file_prefix}.{pid}.log"

    def _get_handler(self) -> logging.FileHandler:
        pid = os.getpid()
        if self._handler is None or self._pid != pid:
            if self._handler is not None:
                self._handler.close()
            self.log_dir.mkdir(parents=True, exist_ok=True)
            handler = logging.FileHandler(
                self._build_path(pid),
                encoding=self.encoding,
                delay=self.delay,
            )
            if self.formatter is not None:
                handler.setFormatter(self.formatter)
            self._handler = handler
            self._pid = pid
        return self._handler

    def setFormatter(self, fmt: logging.Formatter | None) -> None:
        super().setFormatter(fmt)
        if self._handler is not None:
            self._handler.setFormatter(fmt)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._get_handler().emit(record)
        except Exception:
            self.handleError(record)

    def flush(self) -> None:
        if self._handler is not None:
            self._handler.flush()

    def close(self) -> None:
        try:
            if self._handler is not None:
                self._handler.close()
        finally:
            self._handler = None
            self._pid = None
            super().close()


def _resolve_log_dir(log_dir: str | Path | None = None) -> Path:
    if log_dir is None:
        env_log_dir = os.getenv("OPENSEARCH_LOG_DIR")
        if env_log_dir:
            log_dir = env_log_dir
        else:
            log_dir = DEFAULT_LOG_DIR
    return Path(log_dir)


def _has_stream_handler(logger: logging.Logger) -> bool:
    return any(
        isinstance(handler, logging.StreamHandler)
        and not isinstance(handler, (logging.FileHandler, PIDFileHandler))
        for handler in logger.handlers
    )


def _has_pid_file_handler(
    logger: logging.Logger,
    log_dir: Path,
    file_prefix: str,
) -> bool:
    return any(
        isinstance(handler, PIDFileHandler)
        and handler.log_dir == log_dir
        and handler.file_prefix == file_prefix
        for handler in logger.handlers
    )


def _has_fixed_file_handler(logger: logging.Logger, log_path: Path) -> bool:
    return any(
        isinstance(handler, logging.FileHandler)
        and Path(handler.baseFilename) == log_path
        for handler in logger.handlers
    )


def _remove_null_handlers(logger: logging.Logger) -> None:
    for handler in list(logger.handlers):
        if isinstance(handler, logging.NullHandler):
            logger.removeHandler(handler)


def configure_logging(
    *,
    name: str | None = None,
    level: int | str | None = None,
    fmt: str = DEFAULT_FORMAT,
    add_handler: bool = False,
    add_file_handler: bool = True,
    log_dir: str | Path | None = None,
    file_prefix: str = LOGGER_NAME,
    per_process: bool = True,
    propagate: bool = True,
) -> logging.Logger:
    logger = get_logger(name)
    logger.setLevel(_resolve_level(level))
    logger.propagate = propagate

    actual_log_dir = _resolve_log_dir(log_dir)

    if add_file_handler:
        _remove_null_handlers(logger)
        if per_process:
            if not _has_pid_file_handler(logger, actual_log_dir, file_prefix):
                handler = PIDFileHandler(actual_log_dir, file_prefix=file_prefix)
                handler.setFormatter(logging.Formatter(fmt))
                logger.addHandler(handler)
        else:
            actual_log_dir.mkdir(parents=True, exist_ok=True)
            log_path = actual_log_dir / f"{file_prefix}.log"
            if not _has_fixed_file_handler(logger, log_path):
                handler = logging.FileHandler(log_path, encoding="utf-8", delay=True)
                handler.setFormatter(logging.Formatter(fmt))
                logger.addHandler(handler)

    if add_handler and not _has_stream_handler(logger):
        _remove_null_handlers(logger)
        stream_handler = logging.StreamHandler()
        stream_handler.setFormatter(logging.Formatter(fmt))
        logger.addHandler(stream_handler)

    return logger


def summarize_result(result: Any) -> Any:
    if isinstance(result, tuple) and len(result) == 2 and isinstance(result[1], list):
        return {
            "success_count": result[0],
            "error_count": len(result[1]),
        }

    if not isinstance(result, dict):
        return result

    summary: dict[str, Any] = {}
    for key in (
        "_index",
        "_id",
        "_version",
        "result",
        "found",
        "count",
        "acknowledged",
        "errors",
        "took",
        "timed_out",
    ):
        if key in result:
            summary[key] = result[key]

    hits = result.get("hits")
    if isinstance(hits, dict):
        total = hits.get("total")
        if isinstance(total, dict):
            summary["hits_total"] = total.get("value")
        elif total is not None:
            summary["hits_total"] = total

    aggregations = result.get("aggregations")
    if isinstance(aggregations, dict):
        summary["aggregations"] = list(aggregations.keys())

    return summary or result


def log_result(
    logger: logging.Logger,
    action: str,
    result: Any,
    **context: Any,
) -> Any:
    payload = {
        "action": action,
        **{key: value for key, value in context.items() if value is not None},
        "result": summarize_result(result),
    }
    logger.info("%s", payload)
    return result


package_logger = get_logger()
if not package_logger.handlers:
    package_logger.addHandler(logging.NullHandler())
