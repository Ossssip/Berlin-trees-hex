"""
pipeline/aggregate_all.py
-------------------------
Runs aggregate_h3 and aggregate_admin in parallel using ThreadPoolExecutor.

Both scripts open the DuckDB database read-only and write to separate parquet
files, so they can run concurrently without any locking conflicts.

Run:
    conda run -n berlin_trees python pipeline/aggregate_all.py
"""

import logging
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import aggregate_admin
import aggregate_h3

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

JOBS = {
    "aggregate_h3": aggregate_h3.main,
    "aggregate_admin": aggregate_admin.main,
}

if __name__ == "__main__":
    log.info("Running %d aggregation jobs in parallel ...", len(JOBS))
    with ThreadPoolExecutor(max_workers=len(JOBS)) as executor:
        futures = {executor.submit(fn): name for name, fn in JOBS.items()}
        for future in as_completed(futures):
            name = futures[future]
            try:
                future.result()
                log.info("%s completed", name)
            except Exception as exc:
                raise RuntimeError(f"aggregation job '{name}' failed") from exc
    log.info("All aggregation jobs done.")
