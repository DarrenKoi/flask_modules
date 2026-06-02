"""
template / mapped_batch_template.

Split a large in-memory job across MANY small pods to stay under a fixed,
unknown pod memory limit — using dynamic task mapping (``.expand()``).

WHY THIS EXISTS
  On this platform every task instance runs in its OWN pod (KubernetesExecutor),
  with its OWN ephemeral filesystem and its OWN hard memory limit (a cgroup
  limit). A task that builds "tons of files in memory" at once gets OOM-killed:
  the pod dies with exit 137 and Airflow logs the misleading
  "state mismatch ... Pod failed because of None".

  You cannot raise the pod's memory limit (that's a server setting you don't
  control). So the ONLY lever is to make each pod touch LESS data. Dynamic task
  mapping does exactly that: partition the work list into batches and let Airflow
  run each batch as a separate mapped task instance — i.e. a separate pod. Each
  pod's peak memory becomes "one batch", not "the whole job".

  This is the form of "separate the DAG" that actually reduces memory. Splitting
  one task into sequential download->process tasks does NOT help (and breaks,
  because pods don't share a filesystem).

THE TWO RULES THAT MAKE IT WORK (non-negotiable on this platform)
  1. Each mapped task SAVES its slice's output to durable storage (MinIO /
     OpenSearch) IN-PROCESS, then drops it. Pods don't share disk, so you can't
     hand files to another task. Save where you produce.
  2. Tasks RETURN only a tiny summary (counts / keys / failures) via XCom —
     NEVER file contents. XCom goes to the Airflow metadata DB; bytes there are
     fatal.
  And the make-or-break inside the loop: produce one item -> save it -> let it
  go out of scope -> next. Do NOT append items to an outer list. An accumulator
  that outlives the loop defeats the whole point — peak climbs back to the
  whole-job size and you OOM again.

Use this when:
  - One task processes a big list (hosts, files, equipment, time-slices) and
    OOMs because it holds everything at once.
  - The per-item work is independent (order doesn't matter across items).

Do NOT use this when:
  - The job genuinely needs every item in memory together (true cross-item
    aggregation). Then aggregate by reading back from MinIO/OpenSearch instead.

How to adapt:
  1. Copy into dags/<topic>/<name>_dag.py and rename the dag_id.
  2. Replace list_targets() with your real work list (Variable / DB / listing).
  3. Replace msr_check_one() with your per-item logic — the per-item slice of
     msr_check_from_daily_log. Save each result inside it; return a small dict.
  4. Tune BATCH_SIZE DOWN until runs stop OOMing (you can't see the limit, so
     bias small). Tune MAX_PODS_AT_ONCE to cap how many pods run concurrently.

This file lives OUTSIDE airflow_mgmt/dags/ so Airflow does not auto-load it.
"""

import logging
import os
import sys
from datetime import datetime
from pathlib import Path

from airflow.sdk import dag, task

log = logging.getLogger(__name__)


# ── sys.path bootstrap ──────────────────────────────────────────────────────
# Walk parents for the marker file and put that dir on sys.path so repo-local
# packages (minio_handler/, scripts/, utils/, and the root-level ftp_handler/)
# import as top-level names. REPL-safe via the NameError fallback.
def _find_root(marker: str = "project_root.txt") -> Path:
    try:
        start = Path(__file__).resolve().parent
    except NameError:  # REPL / python -c / exec()
        start = Path.cwd().resolve()
    for p in (start, *start.parents):
        if (p / marker).is_file():
            return p
    raise RuntimeError(f"{marker!r} not found above {start}")


ROOT_DIR = _find_root()
# ftp_handler / ops_store / minio_handler live at the repo root (parent of the
# airflow_mgmt marker dir); add both so either layout imports cleanly.
REPO_ROOT = ROOT_DIR.parent if (ROOT_DIR.parent / "ftp_handler").is_dir() else ROOT_DIR
for _p in (REPO_ROOT, ROOT_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
# ────────────────────────────────────────────────────────────────────────────

from minio_handler import MinioObject  # noqa: E402

# ── knobs (env-overridable; no server access needed — these are DAG code) ────
# Items per pod. SMALLER = less peak RAM per pod = safer under the unknown
# limit, but more pods. Start small and only raise it if runs are stable.
BATCH_SIZE = int(os.getenv("MSR_BATCH_SIZE", "20"))
# Cap how many mapped pods run AT ONCE. Limits cluster-wide memory pressure and
# keeps you a good citizen on a shared server. This is a task attribute, not a
# server setting — fully under your control.
MAX_PODS_AT_ONCE = int(os.getenv("MSR_MAX_PODS_AT_ONCE", "4"))
MINIO_BUCKET = os.getenv("MSR_BUCKET", "msr-check")
# Fail the DAG only on a systemic problem, not on a few expected per-item errors.
FAILURE_THRESHOLD = float(os.getenv("MSR_FAILURE_THRESHOLD", "0.2"))


def _chunked(seq: list, size: int) -> list[list]:
    """Split a flat list into consecutive batches of at most ``size``."""
    return [seq[i : i + size] for i in range(0, len(seq), size)]


def msr_check_one(item: dict, storage: MinioObject) -> dict:
    """ONE unit of work — the per-item slice of msr_check_from_daily_log.

    This is YOUR seam. Replace the body with the per-item version of your
    office function. The critical change vs. the OOMing original: do the work
    for a SINGLE item, SAVE its output to MinIO here, and return only a small
    summary. The bytes/objects you build here die when this function returns —
    that is what keeps peak memory flat.

    Returns a small JSON-serializable dict (no file contents).
    """
    # --- replace this stub -------------------------------------------------
    # data = build_files_for(item)            # whatever msr_check produces
    # key = f"{item['date']}/{item['id']}.parquet"
    # storage.put_dataframe(key, data)        # SAVE NOW, then `data` is dropped
    # return {"id": item["id"], "key": key, "ok": True}
    raise NotImplementedError("plug in your per-item msr_check logic here")
    # -----------------------------------------------------------------------


@dag(
    dag_id="template_mapped_batch",
    description="Template: fan a big in-memory job across small pods via .expand()",
    start_date=datetime(2026, 1, 1),
    schedule=None,
    catchup=False,
    max_active_runs=1,
    tags=["template", "mapping", "memory"],
)
def template_mapped_batch() -> None:

    @task
    def list_targets() -> list[dict]:
        """Build the FULL work list — small metadata only, never file bytes.

        Real DAGs read this from an Airflow Variable, a DB query, or an FTP/MinIO
        listing pass. Each element is one unit msr_check_one() will handle.
        """
        # Example shape; replace with your real targets for the daily log.
        return [{"id": f"eqp{i:03d}", "date": "2026-06-02"} for i in range(200)]

    @task
    def make_batches(items: list[dict]) -> list[list[dict]]:
        """Partition the work list. Each returned batch becomes ONE mapped pod."""
        batches = _chunked(items, BATCH_SIZE)
        log.info("split %d items into %d batches of <=%d", len(items), len(batches), BATCH_SIZE)
        return batches

    # max_active_tis_per_dag caps concurrent mapped instances => concurrent pods.
    @task(max_active_tis_per_dag=MAX_PODS_AT_ONCE)
    def process_batch(batch: list[dict]) -> dict:
        """Runs in ITS OWN pod, sees ONLY this batch. Peak RAM = one item.

        The flush-and-drop loop is the whole trick: each item is produced, saved
        inside msr_check_one(), then released before the next item. Nothing is
        accumulated across items, so memory stays flat no matter how big the
        daily log is.
        """
        # One client per pod; minio-py's object client is thread-safe and pooled.
        storage = MinioObject(bucket=MINIO_BUCKET)

        ok = 0
        failures: list[dict] = []
        for item in batch:
            try:
                msr_check_one(item, storage)  # produce -> SAVE -> drop
                ok += 1
            except Exception as exc:  # one bad item never sinks the batch
                log.exception("item failed: %s", item.get("id"))
                failures.append({"id": item.get("id"), "error": f"{type(exc).__name__}: {exc}"})
            # `item`'s produced data is out of scope here; the next loop reuses
            # the freed memory. Append ONLY tiny summaries to `failures`.

        # Return a small summary, NOT the files. This rides XCom (metadata DB).
        return {"ok": ok, "ng": len(failures), "failures": failures}

    @task
    def report(summaries: list[dict]) -> dict:
        """Reduce step: aggregate every pod's summary, alert only on a systemic
        failure. ``summaries`` is the list of all mapped return values."""
        total_ok = sum(s["ok"] for s in summaries)
        total_ng = sum(s["ng"] for s in summaries)
        attempted = total_ok + total_ng
        ratio = total_ng / attempted if attempted else 0.0
        result = {"pods": len(summaries), "ok": total_ok, "ng": total_ng, "failure_ratio": round(ratio, 3)}
        log.info("msr_check summary: %s", result)

        if total_ok == 0 or ratio > FAILURE_THRESHOLD:
            raise RuntimeError(f"msr_check systemic failure: {result} (threshold={FAILURE_THRESHOLD})")
        return result

    targets = list_targets()
    batches = make_batches(targets)
    # .expand() turns the list of batches into N mapped task instances (N pods).
    summaries = process_batch.expand(batch=batches)
    # Pass the mapped task's collected output into a normal task to reduce.
    report(summaries)


template_mapped_batch()
