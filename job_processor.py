import os
import json
import threading
import time
import logging
from concurrent.futures import ThreadPoolExecutor
from db import fetch_and_claim_pending, update_status

from job_executor import execute_job

logger = logging.getLogger(__name__)


def _worker_loop(max_parallel: int, poll_interval: float) -> None:
    """Main worker loop for job processing.

    Continuously polls for pending jobs, claims them, and submits to thread pool.
    Implements backpressure when max parallel jobs are reached.

    Args:
        max_parallel: Maximum number of parallel job workers
        poll_interval: Time to sleep between polls (seconds)
    """
    with ThreadPoolExecutor(max_workers=max_parallel) as executor:
        pending_futures = set()

        while True:
            try:
                # Clean up completed futures
                pending_futures = {f for f in pending_futures if not f.done()}
                running_count = len(pending_futures)

                # Calculate available capacity
                available_slots = max_parallel - running_count

                # Wait if at max capacity
                if available_slots <= 0:
                    logger.info(
                        "Max parallel jobs (%d/%d) reached. Waiting for slot...",
                        running_count,
                        max_parallel,
                    )
                    time.sleep(poll_interval)
                    continue

                # Fetch only up to available slots
                batch = fetch_and_claim_pending(available_slots)
                logger.debug(
                    "Fetched %d jobs. Running: %d/%d",
                    len(batch),
                    running_count,
                    max_parallel,
                )

                if not batch:
                    time.sleep(poll_interval)
                    continue

                for job_id, payload_str in batch:
                    try:
                        payload = json.loads(payload_str)
                    except Exception:
                        logger.warning("Failed to parse payload for job %s", job_id)
                        update_status(job_id, "failed")
                        continue

                    def _run(jid=job_id, pl=payload):
                        try:
                            logger.info("Starting job %s", jid)
                            execute_job(jid, pl)
                            logger.info("Completed job %s", jid)
                            update_status(jid, "done")
                        except Exception:
                            logger.exception("Failed job %s", jid)
                            update_status(jid, "failed")

                    future = executor.submit(_run)
                    pending_futures.add(future)
                    logger.debug(
                        "Submitted job %s. Queue size: %d/%d",
                        job_id,
                        len(pending_futures),
                        max_parallel,
                    )

            except Exception:
                logger.exception("Error in worker loop")
                time.sleep(poll_interval)


def start_job_processor(app) -> None:
    """Start the background job processor thread.

    Initializes a daemon thread that continuously polls for pending jobs,
    claims them for processing, and submits them to a thread pool for execution.

    Configuration via environment variables:
        - MAX_PARALLEL_PROCESSES: Number of parallel job workers (default: 1)
        - JOB_POLL_INTERVAL_SECONDS: Poll interval in seconds (default: 1.0)

    Args:
        app: Flask application instance (for context)
    """
    max_parallel = int(os.environ.get("MAX_PARALLEL_PROCESSES", "1"))
    if max_parallel < 1:
        max_parallel = 1

    poll_interval = float(os.environ.get("JOB_POLL_INTERVAL_SECONDS", "1.0"))

    t = threading.Thread(
        target=_worker_loop, args=(max_parallel, poll_interval), daemon=True
    )
    t.start()
