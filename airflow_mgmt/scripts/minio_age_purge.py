"""Delete MinIO objects under a prefix whose last_modified is older than N days.

Pure logic — no Airflow imports — so it is importable and runnable from a
plain Python REPL. Two DAGs wrap it today:
  dags/image_cache/minio_purge_image_cache_dag.py   (SEM image cache, 7d)
  dags/msr_pickle/minio_purge_old_pickles_dag.py    (MSR pickles, 61d)

Why this sits next to scripts/minio_partition_purge.py rather than reusing it:
that one walks ``YYYY/MM/DD`` partitions, and ``MinioObject.delete_older_than``
is likewise date-folder based. Neither can select a single object from a store
whose keys carry no date. Age instead comes from each object's
``last_modified``, which the MinIO server stamps at PUT time and returns in the
ordinary list response. Reading it needs only prefix-scoped object access, not
the bucket-level permission a native S3 lifecycle rule would require (the
office account has the former and not the latter).

Prefix composition — the trap worth knowing:
``minio_config.PREFIX`` defaults to ``2067928/``, so a ``MinioObject`` built
without an explicit prefix already carries it. ``prefix`` here is therefore
**relative to that**: pass ``image_cache/``, not ``2067928/image_cache/``, or
``list()`` resolves to ``2067928/2067928/image_cache/`` and quietly matches
nothing. The full ``object_name`` values that come back are handed straight to
``delete_many``, which accepts already-prefixed keys.

``suffix`` narrows a shared prefix to one kind of object. It exists because a
prefix is not always a retention unit: the MSR store keeps the post-processed
pickle and the raw ``.MSR`` original side by side under ``hitachi_sem/``, and a
blind sweep would take both. When in doubt, run dry and read the logged names
before setting it.

Local dry-run from the repo root:
    python -m airflow_mgmt.scripts.minio_age_purge
"""

from datetime import datetime, timedelta, timezone
from typing import Any, Iterator

from minio_handler import MinioObject

# Cap what rides in the returned dict. The DAG returns it as an XCom, and a
# sweep can select six figures of objects — the full name list would bloat the
# XCom store for no operational benefit. Counts drive alerting; the sample is
# there to eyeball that the selection looks sane.
SAMPLE_SIZE = 20


def iter_expired(
    storage: MinioObject,
    cutoff: datetime,
    prefix: str,
    suffix: str | None = None,
) -> Iterator[tuple[str, datetime]]:
    """Yield ``(object_name, last_modified)`` for objects older than ``cutoff``.

    ``recursive=True`` so MinIO returns real objects rather than directory-like
    common prefixes. Those carry ``last_modified=None``, and an entry with no
    timestamp is never *provably* stale — skip it rather than delete it. The
    check also keeps a ``None`` from reaching the comparison below, where it
    would raise and abort the whole sweep before anything is deleted.

    ``suffix`` is applied before the age test so a dry run reports only the
    objects the live run would actually touch.
    """
    for obj in storage.list(prefix=prefix, recursive=True):
        if suffix is not None and not obj.object_name.endswith(suffix):
            continue
        last_modified = getattr(obj, "last_modified", None)
        if last_modified is None:
            continue
        if last_modified.tzinfo is None:
            last_modified = last_modified.replace(tzinfo=timezone.utc)
        if last_modified < cutoff:
            yield obj.object_name, last_modified


def purge_modified_before(
    storage: MinioObject,
    days: int,
    *,
    prefix: str,
    dry_run: bool,
    suffix: str | None = None,
    now: datetime | None = None,
    logger: Any | None = None,
) -> dict:
    """Delete every object under ``prefix`` last modified more than ``days`` ago.

    The cutoff is ``now - days``, compared against each object's UTC
    ``last_modified``, so "older than 7 days" means exactly that to the second —
    unlike the date-folder purge, which rounds to whole calendar days.

    ``prefix`` is relative to the client's ``default_prefix`` (see module
    docstring) and must be non-empty: a root prefix would sweep the entire
    bucket by age, and that bucket also holds data no retention policy covers.
    That is not the sort of environment-specific validation this repo avoids —
    a root prefix is catastrophic everywhere.

    ``suffix``, when given, keeps only keys ending with it. ``logger`` is
    optional — pass an Airflow task logger from the DAG, or leave None for
    stdout (handy in a REPL). ``now`` is injectable so tests can pin the cutoff
    without freezing the clock.
    """
    if not prefix.strip().strip("/"):
        raise ValueError(
            "prefix must be non-empty — a root prefix would sweep the whole bucket"
        )

    log = _make_logger(logger)
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=days)

    expired = list(iter_expired(storage, cutoff, prefix, suffix))
    names = [name for name, _ in expired]
    errors: list[Any] = []

    for name, last_modified in expired[:SAMPLE_SIZE]:
        log(
            f"{'[DRY-RUN] would delete' if dry_run else 'deleting'} {name} "
            f"(last_modified={last_modified.isoformat()})"
        )
    if len(expired) > SAMPLE_SIZE:
        log(f"... and {len(expired) - SAMPLE_SIZE} more")

    if names and not dry_run:
        # delete_many batches into remove_objects — multiple keys per HTTP
        # request rather than a per-object delete loop. It returns per-object
        # error entries; those are NOT deletions, so they are excluded from
        # deleted_count and a fully-failed sweep cannot report as a success.
        errors = list(storage.delete_many(names))
        if errors:
            log(f"errors deleting under {prefix}: {errors}")

    return {
        "cutoff": cutoff.isoformat(),
        "dry_run": dry_run,
        "prefix": prefix,
        "suffix": suffix,
        "candidate_count": len(names),
        "deleted_count": 0 if dry_run else len(names) - len(errors),
        "sample": names[:SAMPLE_SIZE],
        "errors": errors,
    }


def _make_logger(logger: Any | None):
    if logger is None:
        return print
    # Accept either a stdlib Logger or anything with .info() that takes a
    # single string. Airflow task loggers satisfy the .info shape.
    if hasattr(logger, "info"):
        return logger.info
    return logger


if __name__ == "__main__":
    # Local dry-run. Bucket, prefix, suffix and retention are env-driven so this
    # same invocation works against dev and prod MinIO without code edits.
    import os

    bucket = os.getenv("MINIO_BUCKET", "user")
    prefix = os.getenv("PURGE_PREFIX", "image_cache/")
    suffix = os.getenv("PURGE_SUFFIX") or None
    days = int(os.getenv("PURGE_DAYS", "7"))

    storage = MinioObject(bucket=bucket)
    result = purge_modified_before(
        storage, days, prefix=prefix, suffix=suffix, dry_run=True
    )
    print(
        f"\ncutoff={result['cutoff']} "
        f"candidates={result['candidate_count']} "
        f"errors={len(result['errors'])}"
    )
