"""Example: retain date-partitioned MinIO folders with the built-in helpers.

This is the same job as ``minio_partition_purge`` / ``hitachi_sem_partition_purge``,
but the three-level ``YYYY/MM/DD`` walk and the KST cutoff no longer live here —
``minio_handler`` owns them now:

    storage.list_date_folders(base)              # discover (cheap, no file scan)
    storage.delete_older_than(days, base)        # cutoff + delete (KST today)

So a retention script collapses to "name the anchors, set the window, call the
method". The helpers already resolve "today" in KST (Asia/Seoul), the timezone
these partitions are stamped in, so there is no cutoff drift on a UTC worker.

Pure logic — no Airflow imports — so it runs from a plain REPL and a DAG can
import ``purge_all`` / ``inspect_all`` as a thin scheduler wrapper.

Layout this assumes on MinIO (bucket / key):

    2067928 / hitachi_sem/{cdsem,hvsem}/{raw_msr,dict_pkl}/YYYY/MM/DD/<files>

The four parents (sensor x data-kind) are each date-partitioned the same way,
so all four are swept in one run. Each parent is the ``base`` *anchor*: the
three folder levels directly under it are read as year / month / day, and a
literal folder like ``cdsem`` is never mistaken for a date because the anchor
is explicit.

Dry-run by default. Set PURGE_APPLY=1 to actually delete:

    python -m airflow_mgmt.scripts.minio_date_retention_example              # preview
    PURGE_APPLY=1 python -m airflow_mgmt.scripts.minio_date_retention_example  # delete
"""

from typing import Any

from minio_handler import DeleteOlderResult, MinioObject

BUCKET = "2067928"
PREFIX_ROOT = "hitachi_sem"
SENSORS = ("cdsem", "hvsem")
KINDS = ("raw_msr", "dict_pkl")
RETENTION_DAYS = 30


def anchors() -> list[str]:
    """The four date-partitioned parents: sensor x data-kind under the root."""

    return [f"{PREFIX_ROOT}/{s}/{k}" for s in SENSORS for k in KINDS]


def _make_logger(logger: Any | None):
    """Accept an Airflow/stdlib logger, or fall back to ``print`` in a REPL."""

    if logger is None:
        return print
    if hasattr(logger, "info"):
        return logger.info
    return logger


def inspect_all(
    storage: MinioObject,
    *,
    logger: Any | None = None,
) -> dict[str, list]:
    """List the date folders under every anchor without touching anything.

    This is the "take a look first" pass: ``list_date_folders`` only issues
    common-prefix listings (one per year/month level), so it stays cheap even
    when each day holds millions of objects. Returns ``{anchor: [DateFolder]}``
    so a caller can render or assert on the inventory.
    """

    log = _make_logger(logger)
    inventory: dict[str, list] = {}
    for base in anchors():
        folders = storage.list_date_folders(base)
        inventory[base] = folders
        span = (
            f"{folders[0].date} .. {folders[-1].date}" if folders else "(empty)"
        )
        log(f"{base}: {len(folders)} date folders  [{span}]")
    return inventory


def purge_anchor(
    storage: MinioObject,
    base: str,
    *,
    retention_days: int = RETENTION_DAYS,
    dry_run: bool = True,
    logger: Any | None = None,
) -> DeleteOlderResult:
    """Sweep one anchor, keeping the most recent ``retention_days`` days.

    ``delete_older_than`` deletes every folder dated strictly before
    ``today_KST - retention_days`` — the last N days, today included, are kept.
    With ``dry_run=True`` it returns the selection and deletes nothing, so the
    same call previews or executes depending on one flag.
    """

    log = _make_logger(logger)
    result = storage.delete_older_than(
        retention_days, base, dry_run=dry_run
    )
    verb = "would delete" if dry_run else "deleted"
    for folder in result.folders:
        log(f"[{base}] {verb} {folder.path} (date={folder.date})")
    if result.errors:
        log(f"[{base}] {len(result.errors)} remove errors: {result.errors}")
    return result


def purge_all(
    storage: MinioObject,
    *,
    retention_days: int = RETENTION_DAYS,
    dry_run: bool = True,
    logger: Any | None = None,
) -> dict:
    """Purge all four anchors and aggregate the outcome into one summary.

    The candidate folder list is always returned, even on a dry run, so a DAG
    task can push it to XCom or a caller can act on it directly.
    """

    selected: list[str] = []
    errors: list[Any] = []
    for base in anchors():
        result = purge_anchor(
            storage,
            base,
            retention_days=retention_days,
            dry_run=dry_run,
            logger=logger,
        )
        selected.extend(f.path for f in result.folders)
        errors.extend(result.errors)

    return {
        "retention_days": retention_days,
        "dry_run": dry_run,
        "selected_prefixes": selected,
        "selected_count": len(selected),
        "errors": errors,
    }


if __name__ == "__main__":
    import os

    dry_run = os.getenv("PURGE_APPLY") != "1"
    days = int(os.getenv("PURGE_DAYS", str(RETENTION_DAYS)))

    storage = MinioObject(bucket=BUCKET)

    # Look before you sweep.
    inspect_all(storage)

    result = purge_all(storage, retention_days=days, dry_run=dry_run)
    print(
        f"\nbucket={BUCKET} root={PREFIX_ROOT}/ "
        f"retention={result['retention_days']}d "
        f"selected={result['selected_count']} "
        f"errors={len(result['errors'])} dry_run={result['dry_run']}"
    )
