"""Purge old YYYY/MM/DD partitions under the Hitachi SEM trees.

Layout on MinIO (bucket / key):
    user / 2067928/hitachi_sem/{cdsem,hvsem}/{raw_msr,dict_pkl}/YYYY/MM/DD/<files>

There are four parent folders (two sensor types x two data kinds); each is
partitioned by date the same way, so all four are swept by default. Pass
``kinds`` to sweep only one — the scheduled pickle job does, because
``dict_pkl`` and ``raw_msr`` do not share a retention rule.

Also wrapped by dags/msr_pickle/minio_purge_old_pickles_dag.py, so this is no
longer only a one-off; keep purge_hitachi_sem importable and side-effect free.

Keeps the most recent ``RETENTION_DAYS`` days and deletes every partition
dated on or before ``today - RETENTION_DAYS``. "today" is resolved in KST
(Asia/Seoul), the timezone these partitions are stamped in, so the cutoff
does not drift by a day when run on a UTC worker near midnight.

The partition walk is reused from minio_partition_purge so the
directory-style listing (one list call per partition level, not one per
file) lives in a single place. The cutoff is applied here rather than via
the shared purge_older_than() — that function is wired to a live job and
keeps the today-30 partition, which is the opposite of what we want here.

Dry-run by default. Set PURGE_APPLY=1 to actually delete:

    python -m airflow_mgmt.scripts.hitachi_sem_partition_purge              # preview
    PURGE_APPLY=1 python -m airflow_mgmt.scripts.hitachi_sem_partition_purge  # delete
"""

from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from minio_handler import MinioObject

from airflow_mgmt.scripts.minio_partition_purge import (
    _make_logger,
    walk_date_partitions,
)

# "2067928" is a PREFIX inside the "user" bucket, not a bucket of its own —
# minio_config.BUCKET is "user" and minio_config.PREFIX is "2067928/" (office
# confirmed 2026-07-24). MinioObject(bucket="2067928") asks for a bucket that
# does not exist and MinIO answers InvalidBucketName; msr_file's office adapter
# calls out that exact failure mode.
BUCKET = "user"
NAMESPACE = "2067928"
# The namespace is spelled out here because _purge_one_parent calls
# use_prefix(), which REPLACES default_prefix rather than appending to it — a
# bare "hitachi_sem/..." would drop the namespace and address nothing.
PREFIX_ROOT = f"{NAMESPACE}/hitachi_sem"
SENSORS = ("cdsem", "hvsem")
KINDS = ("raw_msr", "dict_pkl")
RETENTION_DAYS = 30
KST = ZoneInfo("Asia/Seoul")


def parent_prefixes(
    prefix_root: str = PREFIX_ROOT,
    kinds: tuple[str, ...] = KINDS,
) -> list[str]:
    """The date-partitioned parents: sensor x data-kind under the root.

    ``kinds`` narrows which data kinds are swept. The pickle and the raw .MSR
    original have different retention rules — the pickle backs a 60-day
    consumer window, the raw text is the 원본 — so a caller that means one must
    be able to say so rather than getting both.
    """

    return [f"{prefix_root}/{s}/{k}" for s in SENSORS for k in kinds]


def kst_today() -> date:
    """Today's calendar date in KST, the partitions' stamping timezone."""

    return datetime.now(KST).date()


def _purge_one_parent(
    storage: MinioObject,
    base_prefix: str,
    cutoff: date,
    *,
    dry_run: bool,
    log: Any,
) -> dict:
    """Sweep a single parent folder. ``storage``'s prefix is set here."""

    storage.use_prefix(base_prefix)
    deleted: list[str] = []
    errors: list[Any] = []

    for partition_date, prefix in walk_date_partitions(storage):
        if partition_date > cutoff:
            continue  # inside the retention window — keep

        full = f"{base_prefix}/{prefix}"
        if dry_run:
            log(f"[DRY-RUN] would delete {full} (date={partition_date})")
            deleted.append(full)
            continue

        # delete_prefix bulk-deletes via remove_objects — many keys per
        # request rather than a per-file delete loop.
        errs = storage.delete_prefix(prefix)
        if errs:
            errors.extend(errs)
            log(f"errors deleting {full}: {errs}")
        else:
            deleted.append(full)
            log(f"deleted {full} (date={partition_date})")

    return {"deleted_prefixes": deleted, "errors": errors}


def purge_hitachi_sem(
    storage: MinioObject,
    *,
    retention_days: int = RETENTION_DAYS,
    kinds: tuple[str, ...] = KINDS,
    prefix_root: str = PREFIX_ROOT,
    today: date | None = None,
    dry_run: bool = True,
    logger: Any | None = None,
) -> dict:
    """Purge the selected parent folders, keeping the most recent N days.

    A partition is deleted when ``(today - partition_date).days >=
    retention_days`` — so with the default 30, the partition dated exactly
    30 days ago is removed and the one 29 days ago is kept. ``today``
    defaults to KST today; pass an explicit ``date`` to make a run
    reproducible (handy in tests). The candidate list is always returned,
    even on a dry run, so callers can act on it.

    ``kinds`` defaults to both data kinds, which is right for a one-off
    reclaim but not for a scheduled job: see parent_prefixes.
    """

    log = _make_logger(logger)
    today = today or kst_today()
    cutoff = today - timedelta(days=retention_days)

    deleted: list[str] = []
    errors: list[Any] = []
    for base_prefix in parent_prefixes(prefix_root, kinds):
        outcome = _purge_one_parent(
            storage, base_prefix, cutoff, dry_run=dry_run, log=log
        )
        deleted.extend(outcome["deleted_prefixes"])
        errors.extend(outcome["errors"])

    return {
        "today": today.isoformat(),
        "cutoff": cutoff.isoformat(),
        "retention_days": retention_days,
        "kinds": list(kinds),
        "dry_run": dry_run,
        "deleted_prefixes": deleted,
        "deleted_count": len(deleted),
        "errors": errors,
    }


if __name__ == "__main__":
    import os

    dry_run = os.getenv("PURGE_APPLY") != "1"
    storage = MinioObject(bucket=BUCKET)
    result = purge_hitachi_sem(storage, dry_run=dry_run)
    print(
        f"\nbucket={BUCKET} root={PREFIX_ROOT}/ "
        f"today(KST)={result['today']} cutoff={result['cutoff']} "
        f"retention={result['retention_days']}d "
        f"candidates={result['deleted_count']} "
        f"errors={len(result['errors'])} dry_run={result['dry_run']}"
    )
