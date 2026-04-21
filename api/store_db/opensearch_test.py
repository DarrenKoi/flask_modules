"""Ad-hoc OpenSearch connectivity / path sanity check.

Run from the project root as a module so the top-level ``paths`` module
is importable::

    python -m api.store_db.opensearch_test

The current repository logging setup writes Flask-oriented application logs
through the root-level ``logging_config.py`` helper. Since this module runs as
an ad-hoc script outside a request/app context, it uses a dedicated named
logger and writes to ``logs/flask/opensearch_test.log``.
"""

from logging_config import configure_logging
from paths import project_root, resolve


LOGGER_NAME = "api.store_db.opensearch_test"


def main() -> None:
    log_dir = resolve("logs", "flask")
    log_path = log_dir / "opensearch_test.log"
    logger = configure_logging(
        log_dir,
        "opensearch_test",
        logger_name=LOGGER_NAME,
        level="INFO",
        propagate=False,
    )

    root = project_root()
    logs_dir = resolve("logs", "flask")
    readme_found = resolve("README.md").is_file()

    logger.info(
        "api.store_db.opensearch_test status project_root=%s logs_dir=%s readme_found=%s",
        root,
        logs_dir,
        readme_found,
    )

    print(f"project root : {root}")
    print(f"logs dir     : {logs_dir}")
    print(f"readme found : {readme_found}")
    print(f"log file     : {log_path}")


if __name__ == "__main__":
    main()
