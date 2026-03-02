from job_types import JobPayload
from typing import Dict, Any
from utils import (
    geojson_to_bbox,
    chunks_bbox,
    clip_geojson_with_sentinelhub_bbox,
    build_process_request,
    create_config,
    stitch_data_grid,
    metric_array_to_rgba,
    create_empty_chunk_data,
)
import logging
import os
import requests
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from sentinelhub import SentinelHubDownloadClient
import numpy as np
from tifffile import imwrite
from PIL import Image
from db import insert_feature_result, get_feature_results
from dotenv import load_dotenv


load_dotenv()
CHUNK_AREA_M_MULTIPLIER = int(os.getenv("CHUNK_AREA_M_MULTIPLIER") or 10000)
MAX_FETCH_PARALLEL_REQUESTS_PER_JOB = int(
    os.getenv("MAX_FETCH_PARALLEL_REQUESTS_PER_JOB") or 2
)

logger = logging.getLogger(__name__)
config = create_config()


# Execute a single chunk of a feature
def process_chunk(
    job_id: str,
    payload: JobPayload,
    feature_idx: int,
    row_idx: int,
    col_idx: int,
    geo: Any,
    chunk: Dict[str, Any],
) -> tuple:
    """Process a single grid chunk.

    The function handles three special cases:
      1. the provided ``geo`` does not intersect the chunk bounding box; in this
         case no request is issued and a placeholder with ``nan`` values is
         returned so the stitched output will contain transparent/nodata
         regions.
      2. the evalscript automatically produces a ``mask`` layer; this is
         retained in the returned data so callers can render cloudy/invalid
         pixels appropriately.
      3. normal processing where a Sentinel‑Hub request is issued and decoded.

    Args:
        job_id: Job identifier
        feature_idx: Feature index
        row_idx: Row index in grid
        col_idx: Column index in grid
        geo: GeoJSON geometry
        chunk: Chunk bbox to process

    Returns:
        ``(row_idx, col_idx, pu_consumed, data_dict)`` where ``data_dict`` is
        the decoded response from Sentinel Hub or a placeholder created by
        ``create_empty_chunk_data``.
    """
    logger.debug(
        "Processing chunk (%d,%d) for feature %d", row_idx, col_idx, feature_idx
    )

    clipped_geo = clip_geojson_with_sentinelhub_bbox(geo, chunk)

    # if clipping produced no geometry we can bail out early
    has_geo = False
    if clipped_geo:
        if isinstance(clipped_geo, dict) and "features" in clipped_geo:
            has_geo = bool(clipped_geo["features"])
        else:
            # geometry-only case; check length/emptiness
            try:
                has_geo = len(clipped_geo) > 0
            except Exception:
                has_geo = True

    if not has_geo:
        logger.debug(
            "Chunk (%d,%d) for feature %d has no overlap; returning placeholder",
            row_idx,
            col_idx,
            feature_idx,
        )
        placeholder = create_empty_chunk_data(
            chunk, payload["resolution"], payload["metrics"]
        )
        return (row_idx, col_idx, 0.0, placeholder)

    # Since mosaicking order is most Recent in SH by default, we use last 5 days window as our date range
    end_time = datetime.fromisoformat(payload["time"])
    start_time = end_time - timedelta(days=7)
    time_range = (
        start_time.isoformat(),
        end_time.isoformat(),
    )

    chunk_request = build_process_request(
        clipped_geo,
        chunk,
        time_range,
        payload["resolution"],
        payload["metrics"],
        config,
    )
    client = SentinelHubDownloadClient()
    response = client.download(chunk_request.download_list, decode_data=False)
    pu_consumed = response[0].headers["x-processingunits-spent"]

    data = response[0].decode()

    logger.debug(
        "Job %s: feature=%d chunk=(%d,%d) clipped_geo_items=%d",
        job_id,
        feature_idx,
        row_idx,
        col_idx,
        len(clipped_geo) if hasattr(clipped_geo, "__len__") else 1,
    )

    return (row_idx, col_idx, pu_consumed, data)


# Executes a single feature with N chunks
def process_feature(
    job_id: str, payload: JobPayload, feature_idx: int, geo: Any
) -> None:
    """Process a single feature by creating grid and processing chunks in parallel.

    Args:
        job_id: Job identifier
        feature_idx: Feature index
        geo: GeoJSON feature
    """
    logger.debug("Processing feature %d", feature_idx)

    # Calculate bbox from geojson
    bbox = geojson_to_bbox(geo)

    # Create grid of chunks
    grid = chunks_bbox(bbox, payload["resolution"] * CHUNK_AREA_M_MULTIPLIER)

    logger.debug("Created grid with %d rows for feature %d", len(grid), feature_idx)

    # Prepare chunk tasks for parallel processing
    chunk_tasks = []
    for row_idx, row in enumerate(grid):
        for col_idx, chunk in enumerate(row):
            chunk_tasks.append((row_idx, col_idx, chunk))

    logger.info(
        "Processing %d chunks in parallel (max %d workers) for feature %d",
        len(chunk_tasks),
        MAX_FETCH_PARALLEL_REQUESTS_PER_JOB,
        feature_idx,
    )

    # Process all chunks in parallel with max workers
    data_grid = [[None] * len(row) for row in grid]
    total_pu_consumed = 0.0

    with ThreadPoolExecutor(
        max_workers=MAX_FETCH_PARALLEL_REQUESTS_PER_JOB
    ) as executor:
        futures = {
            executor.submit(
                process_chunk,
                job_id,
                payload,
                feature_idx,
                row_idx,
                col_idx,
                geo,
                chunk,
            ): (row_idx, col_idx)
            for row_idx, col_idx, chunk in chunk_tasks
        }

        # Collect results as they complete
        for future in futures:
            row_idx, col_idx, pu_consumed, data = future.result()
            data_grid[row_idx][col_idx] = data
            total_pu_consumed += float(pu_consumed)

    logger.info(
        "Job %s: completed feature %d, processed grid size=%dx%d",
        job_id,
        feature_idx,
        len(data_grid),
        len(data_grid[0]) if data_grid and data_grid[0] else 0,
    )

    # after all chunks processed, stitch together per-metric arrays
    try:
        metrics = payload["metrics"]
        # stitch metrics plus mask so we can apply invalid-image logic below
        combined = stitch_data_grid(data_grid, metrics + ["mask"])
        mask_arr = combined.pop("mask", None)

        # ensure results directory exists
        base_dir = os.path.join("results", job_id, str(feature_idx))
        os.makedirs(base_dir, exist_ok=True)
        stored_paths: Dict[str, Dict[str, str]] = {}

        for metric, arr in combined.items():
            tif_path = os.path.join(base_dir, f"{metric}.tif")
            png_path = os.path.join(base_dir, f"{metric}.png")

            # write tiff file only if generate_tiff is True (nans preserved as nodata).
            # Users are expected to interpret NaNs accordingly; we don't alter the underlying float values.
            if payload.get("generate_tiff", False):
                imwrite(tif_path, arr)
            else:
                tif_path = None

            # the mask may be None if the evalscript/dataset changed; handle
            # gracefully by treating every pixel as valid
            local_mask = (
                mask_arr if mask_arr is not None else np.ones_like(arr, dtype=np.uint8)
            )

            # generate RGBA image with transparency (outside geometry) and
            # white invalid pixels
            rgba = metric_array_to_rgba(arr, local_mask, metric)
            Image.fromarray(rgba, mode="RGBA").save(png_path)

            # Store paths only for generated files
            stored_paths[metric] = {}
            if tif_path:
                stored_paths[metric]["tif"] = tif_path
            stored_paths[metric]["png"] = png_path

        # record successful feature result in database
        insert_feature_result(
            job_id, feature_idx, total_pu_consumed, stored_paths, status="completed"
        )
        logger.info(
            "Job %s feature %d: saved %d metrics to %s",
            job_id,
            feature_idx,
            len(stored_paths),
            base_dir,
        )
    except Exception as err:
        # record failure and re-raise
        logger.exception(
            "Job %s feature %d: error combining or saving data",
            job_id,
            feature_idx,
        )
        insert_feature_result(
            job_id,
            feature_idx,
            total_pu_consumed,
            {},
            status="failed",
            error=str(err),
        )
        # propagate the error to caller
        raise


# Execute a job of N features
def _send_webhook(
    webhook_url: str, job_id: str, status: str, feature_results: list, error: str = None
) -> None:
    """Send webhook notification after job completion.

    Args:
        webhook_url: URL to POST the webhook notification
        job_id: Job identifier
        status: Job status ('completed' or 'failed')
        feature_results: List of feature result dictionaries
        error: Error message if job failed
    """
    try:
        payload = {
            "job_id": job_id,
            "status": status,
            "feature_results": feature_results,
        }
        if error:
            payload["error"] = error

        response = requests.post(webhook_url, json=payload, timeout=10)
        response.raise_for_status()
        logger.info(
            "Webhook notification sent successfully for job %s to %s",
            job_id,
            webhook_url,
        )
    except Exception as err:
        logger.error(
            "Failed to send webhook for job %s to %s: %s", job_id, webhook_url, err
        )


def execute_job(job_id: str, payload: JobPayload) -> None:
    """Execute job by processing all features and their chunks.

    If webhook_url is provided in payload, sends completion status and feature results
    to the webhook after job completion or failure.

    Args:
        job_id: Job identifier
        payload: Job payload containing geojson features and optional webhook_url
    """
    logger.info("Starting execute_job for job_id=%s", job_id)
    try:
        features = payload.get("geojson", []) if isinstance(payload, dict) else []
        logger.debug("Payload contains %d geojson features", len(features))

        failed = []
        for idx in range(len(features)):
            geo = features[idx]
            try:
                process_feature(job_id, payload, idx, geo)
            except Exception as exc:
                # feature-specific error already recorded by process_feature
                failed.append((idx, exc))
                logger.error(
                    "Job %s: feature %d failed, continuing to next feature. Error: %s",
                    job_id,
                    idx,
                    exc,
                )
                raise

        logger.info("Completed execute_job for job_id=%s", job_id)

        # Send success webhook if configured
        if payload.get("webhook_url"):
            feature_results = get_feature_results(job_id)
            _send_webhook(payload["webhook_url"], job_id, "completed", feature_results)

        if failed:
            # propagate a summary error so that job processor can mark job as failed
            raise RuntimeError(f"{len(failed)} feature(s) failed during execution")
    except Exception as err:
        logger.exception("Error executing job %s", job_id)

        # Send failure webhook if configured
        if isinstance(payload, dict) and payload.get("webhook_url"):
            feature_results = get_feature_results(job_id)
            _send_webhook(
                payload["webhook_url"],
                job_id,
                "failed",
                feature_results,
                error=str(err),
            )

        raise
