from sentinelhub import (
    BBox,
    CRS,
    SHConfig,
    SentinelHubRequest,
    DataCollection,
    MimeType,
    Geometry,
)
from typing import List, Dict, Any
import math
import geopandas as gpd
from shapely.geometry import box, shape
from job_types import AllowedMetric
import os
import numpy as np
import matplotlib.cm as cm


def chunks_bbox(bbox: BBox, chunk_size_m: float) -> List[List[BBox]]:
    """
    Split a bbox (in meters CRS, e.g., UTM) into smaller square
    bboxes of size chunk_size_m x chunk_size_m.

    Works for rectangular bbox.

    Returns:
        2D list of BBox objects arranged as [row][col],
        starting from top-left, row-wise.
    """

    min_x, min_y, max_x, max_y = bbox
    crs = bbox.crs

    # Calculate width and height
    width = max_x - min_x
    height = max_y - min_y

    # Number of chunks per axis
    n_cols = math.ceil(width / chunk_size_m)
    n_rows = math.ceil(height / chunk_size_m)

    grid: List[List[BBox]] = []

    # Start from top (max_y) going downward
    for row in range(n_rows):
        row_bboxes: List[BBox] = []

        for col in range(n_cols):
            x1 = min_x + col * chunk_size_m
            x2 = min(min_x + (col + 1) * chunk_size_m, max_x)

            y2 = max_y - row * chunk_size_m
            y1 = max(max_y - (row + 1) * chunk_size_m, min_y)

            chunk = BBox(bbox=[x1, y1, x2, y2], crs=crs)
            row_bboxes.append(chunk)

        grid.append(row_bboxes)

    return grid


def clip_geojson_with_sentinelhub_bbox(
    input_geojson: Dict[str, Any], bbox: BBox
) -> Dict[str, Any]:
    if input_geojson.get("type") == "Feature":
        gdf = gpd.GeoDataFrame.from_features([input_geojson])
    elif input_geojson.get("type") == "FeatureCollection":
        gdf = gpd.GeoDataFrame.from_features(input_geojson["features"])
    elif input_geojson.get("type") in ["Polygon", "MultiPolygon"]:
        gdf = gpd.GeoDataFrame(geometry=[shape(input_geojson)], crs="EPSG:4326")
    else:
        raise ValueError("Unsupported GeoJSON type")

    if gdf.crs is None:
        gdf.set_crs("EPSG:4326", inplace=True)

    bbox_crs = bbox.crs.pyproj_crs()
    if gdf.crs != bbox_crs:
        gdf = gdf.to_crs(bbox_crs)

    minx, miny = bbox.lower_left
    maxx, maxy = bbox.upper_right

    bbox_polygon = box(minx, miny, maxx, maxy)
    bbox_gdf = gpd.GeoDataFrame(geometry=[bbox_polygon], crs=bbox_crs)

    clipped = gpd.clip(gdf, bbox_gdf)

    clipped = clipped.to_crs("EPSG:32643")

    return clipped.__geo_interface__


def geojson_to_bbox(geojson: Dict[str, Any]) -> BBox:
    if geojson.get("type") == "Feature":
        gdf = gpd.GeoDataFrame.from_features([geojson])
    elif geojson.get("type") == "FeatureCollection":
        gdf = gpd.GeoDataFrame.from_features(geojson["features"])
    elif geojson.get("type") in ["Polygon", "MultiPolygon"]:
        gdf = gpd.GeoDataFrame(geometry=[shape(geojson)], crs="EPSG:4326")
    else:
        raise ValueError("Unsupported GeoJSON type")

    if gdf.crs is None:
        gdf.set_crs("EPSG:4326", inplace=True)

    utm_crs = gdf.estimate_utm_crs()
    gdf_utm = gdf.to_crs(utm_crs)

    minx, miny, maxx, maxy = gdf_utm.total_bounds

    epsg = utm_crs.to_epsg()
    if epsg is None:
        raise ValueError("Could not determine EPSG for UTM CRS")

    return BBox(bbox=[minx, miny, maxx, maxy], crs=CRS(epsg))


def create_config() -> SHConfig:
    client_id = os.getenv("SH_CLIENT_ID")
    client_secret = os.getenv("SH_CLIENT_SECRET")

    config = SHConfig()
    config.sh_client_id = client_id
    config.sh_client_secret = client_secret
    return config


def generate_evalscript(metrics: List[AllowedMetric]) -> str:
    """
    Generate a Sentinel Hub evalscript that computes multiple spectral indices.

    Args:
        metrics: List of metric types to compute (NDVI, PSRI, NDMI, NDRE, NDWI, TRUE_COLOR)

    Returns:
        Evalscript string that computes all requested metrics and a valid/cloud mask

    Example:
        >>> script = generate_evalscript(["NDVI", "TRUE_COLOR"])
        >>> # Returns script that computes NDVI and RGB values
    """

    # Define required bands for each metric
    metric_bands = {
        "NDVI": {"B04", "B08"},  # Red, NIR
        "PSRI": {"B02", "B04", "B08"},  # Blue, Red, NIR
        "NDMI": {"B08", "B11"},  # NIR, SWIR
        "NDRE": {"B05", "B08"},  # Red Edge, NIR
        "NDWI": {"B03", "B08"},  # Green, NIR
        "TRUE_COLOR": {"B02", "B03", "B04"},  # Blue, Green, Red
    }

    # Collect all required bands
    required_bands = {"dataMask", "SCL"}  # Always need these for masking
    for metric in metrics:
        if metric in metric_bands:
            required_bands.update(metric_bands[metric])

    bands_list_sorted = sorted(list(required_bands))
    # Format as JavaScript array
    input_array = ", ".join([f'"{b}"' for b in bands_list_sorted])

    # Create output objects for each metric
    output_configs = []

    for metric in metrics:
        if metric == "TRUE_COLOR":
            output_configs.append(
                f'{{ id: "{metric}", bands: 3, sampleType: "FLOAT32" }}'
            )
        else:
            output_configs.append(
                f'{{ id: "{metric}", bands: 1, sampleType: "FLOAT32" }}'
            )

    output_configs.append('{ id: "mask", bands: 1, sampleType: "UINT8" }')

    # Generate metric calculation code
    metric_calcs = []
    for metric in metrics:
        if metric == "NDVI":
            metric_calcs.append(
                "let denom = samples.B08 + samples.B04; let ndvi = denom !== 0 ? (samples.B08 - samples.B04) / denom : 0;"
            )
        elif metric == "PSRI":
            # Use Red Edge (B05) in denominator for PSRI and add small epsilon
            # to avoid divide-by-zero in the evalscript environment.
            metric_calcs.append(
                "let psri = (samples.B04 - samples.B02) / (samples.B05 + 1e-6);"
            )
        elif metric == "NDMI":
            metric_calcs.append(
                "let ndmi = (samples.B08 - samples.B11) / (samples.B08 + samples.B11 + 1e-6);"
            )
        elif metric == "NDRE":
            metric_calcs.append(
                "let ndre = (samples.B08 - samples.B05) / (samples.B08 + samples.B05 + 1e-6);"
            )
        elif metric == "NDWI":
            metric_calcs.append(
                "let ndwi = (samples.B03 - samples.B08) / (samples.B03 + samples.B08 + 1e-6);"
            )
        elif metric == "TRUE_COLOR":
            metric_calcs.append(
                "let gain = 2.5; let trueColor = [samples.B04*gain, samples.B03*gain, samples.B02*gain];"
            )

    # Generate return object properties
    return_props = []
    for metric in metrics:
        if metric == "TRUE_COLOR":
            return_props.append(f" {metric}: trueColor")
        else:
            return_props.append(f" {metric}: [{metric.lower()}]")
    return_props.append(" mask: [valid]")

    metric_calcs_str = "\n          ".join(metric_calcs)
    return_props_str = ",\n          ".join(return_props)
    output_configs_str = ",\n              ".join(output_configs)

    evalscript = f"""
        //VERSION=3
        function setup() {{
          return {{
            input: [{input_array}],
            output: [
              {output_configs_str}
            ]
          }};
        }}

        function evaluatePixel(samples) {{
          let isCloud = [3, 8, 9, 10].includes(samples.SCL);
          let valid = (samples.dataMask === 1 && !isCloud) ? 1 : 0;
          {metric_calcs_str}
          return {{{return_props_str}}};
        }}
        """

    return evalscript


def build_process_request(
    geojson: Dict[str, Any],
    bbox: BBox,
    time_range,
    resolution: float,
    metrics: List[str],
    config,
):
    """
    Build a Sentinel Hub process request using:
      - Clipped GeoJSON geometry
      - Time range
      - Spatial resolution (meters per pixel)
      - Metrics list (NDVI, NDMI, TRUE_COLOR, etc.)
      - Sentinel Hub config

    Args:
        geojson: Clipped GeoJSON from clip_geojson_with_sentinelhub_bbox
        time_range: Tuple (start_date, end_date)
        resolution: Spatial resolution in meters (e.g., 10)
        metrics: List of metrics to compute
        config: Sentinel Hub configuration

    Returns:
        SentinelHubRequest object
    """

    # Convert GeoJSON to SentinelHub Geometry
    geojson_geometry = (
        geojson["features"][0]["geometry"] if geojson["features"] else None
    )

    geometry = (
        Geometry(geojson_geometry, crs="EPSG:32643") if geojson_geometry else None
    )

    # Generate evalscript dynamically
    evalscript = generate_evalscript(metrics)

    # Build response definitions dynamically
    responses = []

    for metric in metrics:
        responses.append(SentinelHubRequest.output_response(metric, MimeType.TIFF))

    # Always return mask
    responses.append(SentinelHubRequest.output_response("mask", MimeType.TIFF))

    request = SentinelHubRequest(
        evalscript=evalscript,
        input_data=[
            SentinelHubRequest.input_data(
                data_collection=DataCollection.SENTINEL2_L2A,
                time_interval=time_range,
            )
        ],
        responses=responses,
        geometry=geometry,
        resolution=(resolution, resolution),
        config=config,
        bbox=bbox,
    )

    return request


# helper to stitch chunk results matrix into full-metric arrays
def stitch_data_grid(
    data_grid: List[List[Dict[str, np.ndarray]]],
    metrics: List[str],
) -> Dict[str, np.ndarray]:
    if not metrics:
        raise ValueError("Metrics list is empty")

    if not data_grid or not data_grid[0]:
        raise ValueError("Data grid is empty")

    n_rows = len(data_grid)
    n_cols = len(data_grid[0])

    result: Dict[str, np.ndarray] = {}

    for metric in metrics:
        key = f"{metric}.tif"

        # --- Determine target width per column ---
        col_widths = []
        for col in range(n_cols):
            widths = []
            for row in range(n_rows):
                cell = data_grid[row][col]
                if cell is None:
                    raise ValueError("Missing data for a chunk")
                if key not in cell:
                    raise KeyError(
                        f"{key} not found. Available keys: {list(cell.keys())}"
                    )
                widths.append(cell[key].shape[1])
            col_widths.append(max(widths))

        # --- Determine target height per row ---
        row_heights = []
        for row in range(n_rows):
            heights = []
            for col in range(n_cols):
                heights.append(data_grid[row][col][key].shape[0])
            row_heights.append(max(heights))

        padded_rows = []

        # --- Pad each cell only to its row/column target ---
        for row in range(n_rows):
            padded_cells = []

            for col in range(n_cols):
                arr = data_grid[row][col][key]

                target_h = row_heights[row]
                target_w = col_widths[col]

                pad_h = target_h - arr.shape[0]
                pad_w = target_w - arr.shape[1]

                if pad_h > 0 or pad_w > 0:
                    fill = 0 if metric == "mask" else np.nan

                    arr = np.pad(
                        arr,
                        ((0, pad_h), (0, pad_w)) + ((0, 0),) * (arr.ndim - 2),
                        constant_values=fill,
                    )

                padded_cells.append(arr)

            # concatenate horizontally per row
            padded_rows.append(np.concatenate(padded_cells, axis=1))

        # concatenate vertically
        result[metric] = np.concatenate(padded_rows, axis=0)

    return result


def metric_array_to_rgba(arr: np.ndarray, mask: np.ndarray, metric: str):
    # Build 2D NaN mask
    if arr.ndim == 3:
        nan_mask = np.isnan(arr).any(axis=2)
    else:
        nan_mask = np.isnan(arr)
    if metric == "TRUE_COLOR":
        img = arr

        if np.issubdtype(img.dtype, np.floating):
            img8 = (img * 255.0).clip(0, 255).astype("uint8")
        else:
            img8 = img.astype("uint8")

    else:
        band = arr
        if band.ndim > 2:
            band = band.squeeze()

        # Consider only finite, non-NaN pixels as valid
        valid = np.isfinite(band) & (~np.isnan(band))

        norm = np.zeros_like(band, dtype=np.float32)

        if np.any(valid):
            band_valid = band[valid].astype(np.float64)
            mn = float(np.nanmin(band_valid))
            mx = float(np.nanmax(band_valid))
            denom = mx - mn

            # Only normalize when we have a positive finite dynamic range
            if np.isfinite(denom) and denom > 0:
                tmp = np.empty_like(band, dtype=np.float64)
                np.subtract(band, mn, out=tmp, where=valid)
                np.divide(tmp, denom, out=tmp, where=valid)
                norm[valid] = tmp[valid].astype(np.float32)

        cmap_lookup = {
            "NDVI": cm.RdYlGn,
            "NDMI": cm.BrBG,
            "NDWI": cm.Blues,
            "NDRE": cm.YlGn,
            "PSRI": cm.inferno,
        }

        cmap = cmap_lookup.get(metric, cm.viridis)
        colored = cmap(norm)

        img8 = (colored[:, :, :3] * 255).astype("uint8")

    # Create alpha channel (transparent where NaN)
    alpha = (~nan_mask).astype("uint8") * 255

    # Stack RGBA
    if img8.ndim == 2:
        rgba = np.stack([img8, img8, img8, alpha], axis=2)
    else:
        rgba = np.concatenate([img8, alpha[..., None]], axis=2)

    invalid = (mask == 0) & (~nan_mask)
    rgba[invalid] = [255, 255, 255, 255]

    return rgba


# Execute a chunk consisting N metrics (NDVI, EVI etc.)
def create_empty_chunk_data(
    chunk: Any, resolution: float, metrics: List[str]
) -> Dict[str, Any]:
    """Generate placeholder data for a chunk with no overlap.

    The output mimics the structure returned by ``SentinelHubDownloadClient.decode()``
    so that downstream stitching logic can handle it transparently.  All metric
    arrays are filled with ``np.nan`` (so that TIFF nodata values can be traced)
    and the associated mask is set to zero (invalid).

    Args:
        chunk: Bounding box for the chunk being processed (usually a
            ``sentinelhub.BBox`` instance, but a plain list/tuple is also
            supported).
        resolution: Spatial resolution (meters per pixel) used for the request.
        metrics: List of requested metric names.

    Returns:
        Dictionary mapping ``"<metric>.tif"`` (and ``"mask.tif"``) to numpy
        arrays of the correct shape.
    """
    # obtain numerical bbox coordinates regardless of object type
    if hasattr(chunk, "bbox"):
        x1, y1, x2, y2 = chunk.bbox
    else:
        x1, y1, x2, y2 = chunk

    width = int(np.ceil((x2 - x1) / resolution))
    height = int(np.ceil((y2 - y1) / resolution))

    placeholder: Dict[str, Any] = {}
    for metric in metrics:
        if metric == "TRUE_COLOR":
            placeholder[f"{metric}.tif"] = np.full(
                (height, width, 3), np.nan, dtype=np.float32
            )
        else:
            placeholder[f"{metric}.tif"] = np.full(
                (height, width), np.nan, dtype=np.float32
            )

    # mask always returned by our evalscript; mark entire tile invalid
    placeholder["mask.tif"] = np.zeros((height, width), dtype=np.uint8)
    return placeholder
