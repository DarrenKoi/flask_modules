"""Hourly Redis-backed item check scheduler.

This DAG is intentionally separate from the two-day item-list refresh job.
The refresh job should load a new Redis generation. This DAG wakes up hourly
and checks a bounded number of items from the current generation.
"""

import os
import sys
from datetime import datetime
from pathlib import Path

from airflow.sdk import DAG, task


def _find_root(marker: str = "project_root.txt") -> Path:
    try:
        start = Path(__file__).resolve().parent
    except NameError:
        start = Path.cwd().resolve()
    for p in (start, *start.parents):
        if (p / marker).is_file():
            return p
    raise RuntimeError(f"{marker!r} not found above {start}")


ROOT_DIR = _find_root()
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from scripts.item_check_queue import ItemCheckConfig, ItemCheckQueue  # noqa: E402
from scripts.item_check_queue import redis_client_from_env, run_check_chunk  # noqa: E402


CHUNK_TASKS = int(os.getenv("ITEM_CHECK_CHUNK_TASKS", "4"))


with DAG(
    dag_id="item_check_hourly",
    description="Hourly Redis-backed item checks for the current two-day generation",
    start_date=datetime(2026, 1, 1),
    schedule="0 * * * *",
    catchup=False,
    max_active_runs=1,
    max_active_tasks=CHUNK_TASKS,
    tags=["item-check", "redis"],
) as dag:
    @task
    def select_generation() -> str | None:
        config = ItemCheckConfig.from_env()
        client = redis_client_from_env(config)
        return ItemCheckQueue(client, config).current_generation()

    @task
    def run_chunk(chunk_index: int, generation: str | None) -> dict:
        config = ItemCheckConfig.from_env()
        return run_check_chunk(
            chunk_index,
            generation=generation,
            use_current_generation=False,
            config=config,
        )

    selected_generation = select_generation()
    for i in range(CHUNK_TASKS):
        run_chunk.override(task_id=f"check_chunk_{i + 1:02d}")(
            i,
            selected_generation,
        )
