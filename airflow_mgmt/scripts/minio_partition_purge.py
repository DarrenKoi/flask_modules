"""Walk YYYY/MM/DD MinIO partitions and delete the ones older than N days.

Pure logic — no Airflow imports — so it is importable and runnable from a
plain Python REPL. The DAG at dags/recipe_logs/minio_purge_old_logs_dag.py
is just a thin scheduler wrapper around purge_older_than() defined here.

Local dry-run from the repo root:
    python -m airflow_mgmt.scripts.minio_partition_purge

That uses whichever minio_handler/ Python finds first on sys.path. From
the repo root, that is the root-level minio_handler/. On an Airflow
worker (after the sys.path bootstrap puts airflow_mgmt/ first), the same
import resolves to airflow_mgmt/minio_handler/. Same code path either
way — keep the two minio_handler copies in sync to preserve that.
"""

from datetime import date, timedelta
from typing import Any, Iterator

from minio_handler import MinioObject


def walk_date_partitions(storage: MinioObject) -> Iterator[tuple[date, str]]:
    """Yield (partition_date, prefix) for each YYYY/MM/DD partition.

    recursive=False at every level returns directory-like common prefixes
    rather than individual objects — N list calls for N partitions, not
    N list calls for millions of files.
    """
    for y_obj in storage.list(prefix=None, recursive=False):
        y = y_obj.object_name.rstrip("/").split("/")[-1]
        if not (y.isdigit() and len(y) == 4):
            continue

        for m_obj in storage.list(prefix=f"{y}/", recursive=False):
            m = m_obj.object_name.rstrip("/").split("/")[-1]
            if not (m.isdigit() and len(m) == 2):
                continue

            for d_obj in storage.list(prefix=f"{y}/{m}/", recursive=False):
                d = d_obj.object_name.rstrip("/").split("/")[-1]
                if not (d.isdigit() and len(d) == 2):
                    continue
                try:
                    partition_date = date(int(y), int(m), int(d))
                except ValueError:
                    continue  # e.g. 2026/02/30
                yield partition_date, f"{y}/{m}/{d}/"


def purge_older_than(
    storage: MinioObject,
    days: int,
    *,
    dry_run: bool,
    logger: Any | None = None,
) -> dict:
    """Delete every partition strictly older than `days` days.

    `logger` is optional — pass an Airflow task logger from the DAG, or
    leave None for stdout (handy in a REPL). Either way the candidate
    list is returned so callers can act on it directly.
    """
    log = _make_logger(logger)
    cutoff = date.today() - timedelta(days=days)
    candidates: list[str] = []
    errors: list[Any] = []

    for partition_date, prefix in walk_date_partitions(storage):
        if partition_date >= cutoff:
            continue

        candidates.append(prefix)
        if dry_run:
            log(f"[DRY-RUN] would delete {prefix} (date={partition_date})")
            continue

        # delete_prefix bulk-deletes via remove_objects — multiple keys
        # per HTTP request rather than a per-file delete loop.
        errs = storage.delete_prefix(prefix)
        if errs:
            errors.extend(errs)
            log(f"errors deleting {prefix}: {errs}")
        else:
            log(f"deleted {prefix} (date={partition_date})")

    return {
        "cutoff": cutoff.isoformat(),
        "dry_run": dry_run,
        "candidate_prefixes": candidates,
        "candidate_count": len(candidates),
        "errors": errors,
    }


def _make_logger(logger: Any | None):
    if logger is None:
        return print
    # Accept either a stdlib Logger or anything with .info() that takes
    # a single string. Airflow task loggers satisfy the .info shape.
    if hasattr(logger, "info"):
        return logger.info
    return logger


if __name__ == "__main__":
    # Local dry-run. Bucket and retention are env-driven so this same
    # invocation works against dev and prod MinIO without code edits.
    import os

    bucket = os.getenv("MINIO_BUCKET", "eqp-logs")
    days = int(os.getenv("PURGE_DAYS", "90"))

    storage = MinioObject(bucket=bucket)
    result = purge_older_than(storage, days, dry_run=True)
    print(
        f"\ncutoff={result['cutoff']} "
        f"candidates={result['candidate_count']} "
        f"errors={len(result['errors'])}"
    )
