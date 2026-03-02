from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse
import json

from job_types import JobPayload


ALLOWED_METRICS = {"NDVI", "PSRI", "NDMI", "NDRE", "NDWI", "TRUE_COLOR"}


def _is_valid_url(url: str) -> bool:
    """Validate if a URL is a valid http/https URL.

    Args:
        url: URL string to validate

    Returns:
        True if valid http/https URL, False otherwise
    """
    try:
        p = urlparse(url)
        return p.scheme in {"http", "https"} and bool(p.netloc)
    except Exception:
        return False


def _parse_time(value: Any) -> Optional[datetime]:
    """Parse time value in multiple formats.

    Supports:
        - Unix timestamps (int/float)
        - ISO-8601 strings (with or without Z suffix)
        - Returns UTC datetime

    Args:
        value: Time value to parse

    Returns:
        Datetime in UTC or None if parsing fails
    """
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        except Exception:
            return None

    if isinstance(value, str):
        s = value.strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            else:
                dt = dt.astimezone(timezone.utc)
            return dt
        except Exception:
            return None

    return None


def _extract_geometry(obj: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Extract geometry from GeoJSON Feature, FeatureCollection,
    or return geometry object directly.

    Args:
        obj: GeoJSON Feature, FeatureCollection, or geometry object

    Returns:
        Geometry dictionary or None if not valid
    """
    t = obj.get("type")

    if t == "Feature":
        geom = obj.get("geometry")
        return geom if isinstance(geom, dict) else None

    if t == "FeatureCollection":
        features = obj.get("features")
        if (
            isinstance(features, list)
            and len(features) == 1
            and isinstance(features[0], dict)
        ):
            return _extract_geometry(features[0])
        return None

    if t in {"Polygon", "MultiPolygon"}:
        return obj

    return None


def _validate_single_land_chunk(geom: Dict[str, Any]) -> bool:
    """Validate that geometry represents a single land chunk.

    Must be either a Polygon or MultiPolygon with exactly 1 polygon.

    Args:
        geom: GeoJSON geometry object

    Returns:
        True if valid single land chunk, False otherwise
    """
    gtype = geom.get("type")
    coords = geom.get("coordinates")

    if gtype == "Polygon":
        return isinstance(coords, list) and len(coords) > 0
    if gtype == "MultiPolygon":
        return (
            isinstance(coords, list)
            and len(coords) == 1
            and isinstance(coords[0], list)
            and len(coords[0]) > 0
        )

    return False


def validate_job_request(data: Dict[str, Any]) -> Tuple[bool, Any]:
    """Validate incoming job request and return normalized payload.

    Validates:
        - geojson: non-empty array of single land chunks
        - time: valid ISO-8601 or unix timestamp, not in future
        - webhook_url: optional valid http/https URL
        - resolution: integer between 10-1500
        - Metrices: array of valid metric names
        - generateTiff: optional boolean (default False)

    Args:
        data: Raw request data dictionary

    Returns:
        Tuple of (success: bool, result_or_error: Any)
        If success, returns normalized JobPayload as dict.
        If failed, returns error message string.
    """
    if not isinstance(data, dict):
        return False, "Body must be a JSON object"

    # Accept multiple input shapes:
    # 1) {"geojson": [ ... ], "time": ...}
    # 2) A top-level GeoJSON FeatureCollection object (must include "features")
    # 3) A single GeoJSON Feature object
    if (
        isinstance(data.get("type"), str)
        and data.get("type") == "FeatureCollection"
        and isinstance(data.get("features"), list)
    ):
        geojson = data.get("features")
    elif isinstance(data.get("type"), str) and data.get("type") == "Feature":
        geojson = [data]
    else:
        geojson = data.get("geojson")
    if not isinstance(geojson, list) or len(geojson) == 0:
        return False, "geojson must be a non-empty array"

    normalized_geojson: List[Dict[str, Any]] = []
    for i, item in enumerate(geojson):
        if not isinstance(item, dict):
            return False, f"geojson[{i}] must be an object"
        geom = _extract_geometry(item)
        if geom is None:
            return (
                False,
                f"geojson[{i}] must be a Feature with geometry or a Polygon/MultiPolygon",
            )
        if not _validate_single_land_chunk(geom):
            return (
                False,
                f"geojson[{i}] must contain only 1 chunk of land (Polygon or MultiPolygon with 1 polygon)",
            )
        normalized_geojson.append(item)

    time_value = data.get("time")
    dt = _parse_time(time_value)
    if dt is None:
        return False, "time must be a valid ISO-8601 string or unix timestamp"
    now = datetime.now(timezone.utc)
    if dt > now:
        return False, "time must not be in the future"

    webhook_url = data.get("webhook_url", None)
    if webhook_url is not None:
        if not isinstance(webhook_url, str) or not webhook_url.strip():
            return False, "webhook_url must be a non-empty string when provided"
        if not _is_valid_url(webhook_url):
            return False, "webhook_url must be a valid http/https URL"

    resolution = data.get("resolution", 100)
    if not isinstance(resolution, int):
        return False, "resolution must be an integer"
    if resolution < 10:
        return False, "resolution must be at least 10"
    if resolution > 1500:
        return False, "resolution must be at max 1500"

    metrics = data.get(
        "Metrices", ["NDVI", "PSRI", "NDMI", "NDRE", "NDWI", "TRUE_COLOR"]
    )
    if not isinstance(metrics, list) or len(metrics) == 0:
        return False, "Metrices must be a non-empty array when provided"
    for m in metrics:
        if not isinstance(m, str) or m not in ALLOWED_METRICS:
            return False, f"Invalid metric: {m}"

    generate_tiff = data.get("generateTiff", False)
    if not isinstance(generate_tiff, bool):
        return False, "generateTiff must be a boolean"

    payload = JobPayload(
        geojson=normalized_geojson,
        time=dt.isoformat(),
        webhook_url=webhook_url,
        resolution=resolution,
        metrics=metrics,
        generate_tiff=generate_tiff,
    )

    return True, json.loads(json.dumps(payload.__dict__))
