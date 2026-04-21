"""Ad-hoc OpenSearch connectivity / path sanity check.

Run from the project root as a module so the top-level ``paths`` module
is importable::

    python -m api.store_db.opensearch_test
"""

from paths import project_root, resolve


def main() -> None:
    root = project_root()
    logs_dir = resolve("logs", "opensearch")

    print(f"project root : {root}")
    print(f"logs dir     : {logs_dir}")
    print(f"readme found : {resolve('README.md').is_file()}")


if __name__ == "__main__":
    main()
