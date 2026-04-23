import logging
import os
import tempfile
import unittest
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

from flask import Flask

from logging_config import (
    DEFAULT_RETENTION_DAYS,
    configure_flask_logging,
    configure_logging,
    setup_logger,
    silence_opensearch_client_warnings,
)


class LoggingConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.loggers: list[logging.Logger] = []

    def tearDown(self) -> None:
        for logger in self.loggers:
            for handler in list(logger.handlers):
                handler.close()
                logger.removeHandler(handler)
            logger.setLevel(logging.NOTSET)
            logger.propagate = True

    def test_configure_logging_adds_timed_rotating_handler(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            logger = configure_logging(
                tmp_dir,
                "flask-server",
                logger_name="test.logging_config.file",
                level="DEBUG",
                propagate=False,
            )
            self.loggers.append(logger)

            handlers = [
                handler
                for handler in logger.handlers
                if isinstance(handler, TimedRotatingFileHandler)
            ]

            self.assertEqual(logger.level, logging.DEBUG)
            self.assertFalse(logger.propagate)
            self.assertEqual(len(handlers), 1)
            self.assertEqual(
                Path(handlers[0].baseFilename),
                Path(tmp_dir) / "flask-server.log",
            )
            self.assertEqual(handlers[0].when, "MIDNIGHT")
            self.assertEqual(handlers[0].backupCount, DEFAULT_RETENTION_DAYS)

    def test_configure_logging_writes_under_requested_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            logger = configure_logging(
                tmp_dir,
                "api",
                logger_name="test.logging_config.write",
            )
            self.loggers.append(logger)

            logger.info("hello flask logging")

            for handler in logger.handlers:
                handler.flush()

            expected_path = Path(tmp_dir) / "api.log"
            self.assertTrue(expected_path.exists())
            self.assertIn("hello flask logging", expected_path.read_text(encoding="utf-8"))

    def test_configure_logging_does_not_duplicate_handler_for_same_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            logger = configure_logging(
                tmp_dir,
                "service",
                logger_name="test.logging_config.idempotent",
            )
            self.loggers.append(logger)
            configure_logging(
                tmp_dir,
                "service",
                logger_name="test.logging_config.idempotent",
            )

            handlers = [
                handler
                for handler in logger.handlers
                if isinstance(handler, TimedRotatingFileHandler)
            ]

            self.assertEqual(len(handlers), 1)

    def test_configure_logging_does_not_duplicate_handler_for_same_relative_file(self) -> None:
        original_cwd = Path.cwd()

        with tempfile.TemporaryDirectory() as tmp_dir:
            os.chdir(tmp_dir)
            try:
                logger = configure_logging(
                    "logs/flask",
                    "service",
                    logger_name="test.logging_config.relative",
                )
                self.loggers.append(logger)
                configure_logging(
                    "logs/flask",
                    "service",
                    logger_name="test.logging_config.relative",
                )

                handlers = [
                    handler
                    for handler in logger.handlers
                    if isinstance(handler, TimedRotatingFileHandler)
                ]

                self.assertEqual(len(handlers), 1)
                expected_path = Path(tmp_dir) / "logs" / "flask" / "service.log"
                self.assertEqual(
                    Path(os.path.realpath(handlers[0].baseFilename)),
                    Path(os.path.realpath(expected_path)),
                )
            finally:
                os.chdir(original_cwd)

    def test_configure_flask_logging_uses_app_logger(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            app = Flask("test_flask_logging")
            logger = configure_flask_logging(app, tmp_dir, "server")
            self.loggers.append(app.logger)

            self.assertIs(logger, app.logger)
            self.assertTrue(
                any(
                    isinstance(handler, TimedRotatingFileHandler)
                    and Path(handler.baseFilename) == Path(tmp_dir) / "server.log"
                    for handler in app.logger.handlers
                )
            )

    def test_setup_logger_uses_legacy_signature(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            logger = setup_logger(tmp_dir, "legacy_service")
            self.loggers.append(logger)

            self.assertEqual(logger.name, "legacy_service")
            self.assertFalse(logger.propagate)
            self.assertTrue(
                any(
                    isinstance(handler, TimedRotatingFileHandler)
                    and Path(handler.baseFilename) == Path(tmp_dir) / "legacy_service.log"
                    for handler in logger.handlers
                )
            )

    def test_setup_logger_creates_missing_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            log_dir = Path(tmp_dir) / "nested" / "service_logs"
            logger = setup_logger(log_dir, "legacy_service")
            self.loggers.append(logger)

            logger.info("create the log directory")

            for handler in logger.handlers:
                handler.flush()

            self.assertTrue(log_dir.is_dir())
            self.assertTrue((log_dir / "legacy_service.log").exists())

    def test_silence_opensearch_client_warnings_sets_error_by_default(self) -> None:
        for name in ("opensearch", "opensearchpy"):
            self.loggers.append(logging.getLogger(name))

        silence_opensearch_client_warnings()

        self.assertEqual(logging.getLogger("opensearch").level, logging.ERROR)
        self.assertEqual(logging.getLogger("opensearchpy").level, logging.ERROR)

    def test_silence_opensearch_client_warnings_accepts_string_level(self) -> None:
        for name in ("opensearch", "opensearchpy"):
            self.loggers.append(logging.getLogger(name))

        silence_opensearch_client_warnings(level="CRITICAL")

        self.assertEqual(logging.getLogger("opensearch").level, logging.CRITICAL)
        self.assertEqual(logging.getLogger("opensearchpy").level, logging.CRITICAL)

    def test_configure_logging_rejects_logger_and_logger_name_together(self) -> None:
        logger = logging.getLogger("test.logging_config.conflict")
        self.loggers.append(logger)

        with self.assertRaises(ValueError):
            configure_logging(
                "logs/flask",
                "service",
                logger=logger,
                logger_name="test.logging_config.conflict",
            )
