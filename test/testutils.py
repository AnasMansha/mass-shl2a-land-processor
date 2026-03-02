import numpy as np
import matplotlib.pyplot as plt
from pyproj import Transformer
from sentinelhub import (
    BBox,
    CRS,
    SHConfig,
    SentinelHubRequest,
    DataCollection,
    MimeType,
)
from typing import List
import math


class VegetationIndexStrategy:
    def get_evalscript(self) -> str:
        raise NotImplementedError


class NDVI(VegetationIndexStrategy):
    def get_evalscript(self) -> str:
        return """
        //VERSION=3
        function setup() {
          return {
            input: ["B04", "B08", "SCL", "dataMask"],
            output: [
              { id: "NDVI", bands: 1, sampleType: "FLOAT32" },
              { id: "mask", bands: 1, sampleType: "UINT8" }
            ]
          };
        }

        function evaluatePixel(samples) {
          let isCloud = [3, 8, 9, 10].includes(samples.SCL);
          let valid = (samples.dataMask === 1 && !isCloud) ? 1 : 0;
          let ndvi = (samples.B08 - samples.B04) / (samples.B08 + samples.B04);
          return { NDVI: [ndvi],
           mask: [valid]};
        }
        """


def create_config(client_id: str, client_secret: str) -> SHConfig:
    config = SHConfig()
    config.sh_client_id = client_id
    config.sh_client_secret = client_secret
    return config


def chunks_bbox(
    bbox: BBox, bbox_area_m: float, chunk_area_m: float
) -> List[List[BBox]]:
    """
    Split a square bbox (in meters, UTM CRS) into a grid of smaller square
    bboxes of size chunk_area_m x chunk_area_m.

    Returns:
        2D list of BBox objects arranged as [row][col],
        starting from top-left, row-wise.
    """

    min_x, min_y, max_x, max_y = bbox
    crs = bbox.crs

    # Number of chunks per axis
    n_chunks = int(math.ceil(bbox_area_m / chunk_area_m))

    grid: List[List[BBox]] = []

    for row in range(n_chunks):
        row_bboxes: List[BBox] = []

        for col in range(n_chunks):
            x1 = min_x + col * chunk_area_m
            y1 = min_y + row * chunk_area_m
            x2 = min(x1 + chunk_area_m, max_x)
            y2 = min(y1 + chunk_area_m, max_y)

            chunk = BBox(bbox=[x1, y1, x2, y2], crs=crs)
            row_bboxes.append(chunk)

        grid.append(row_bboxes)

    return grid


def build_bbox(center_lat: float, center_lon: float, area_size_m: float) -> BBox:
    zone = int((center_lon + 180) / 6) + 1
    epsg = 32600 + zone if center_lat >= 0 else 32700 + zone
    to_utm = Transformer.from_crs("EPSG:4326", f"EPSG:{epsg}", always_xy=True)
    x_center, y_center = to_utm.transform(center_lon, center_lat)
    half = area_size_m / 2
    return BBox(
        bbox=[x_center - half, y_center - half, x_center + half, y_center + half],
        crs=CRS(f"EPSG:{epsg}"),
    )


def build_process_request(
    bbox, time_range, area_size_m, chunk_size_m, index_strategy, config
):
    size = int(area_size_m / chunk_size_m)
    x = SentinelHubRequest(
        evalscript=index_strategy.get_evalscript(),
        input_data=[
            SentinelHubRequest.input_data(
                data_collection=DataCollection.SENTINEL2_L2A,
                time_interval=time_range,
            )
        ],
        responses=[
            SentinelHubRequest.output_response("TRUE_COLOR", MimeType.TIFF),
            SentinelHubRequest.output_response("NDVI", MimeType.TIFF),
            SentinelHubRequest.output_response("mask", MimeType.TIFF),
        ],
        bbox=bbox,
        size=(size, size),
        config=config,
    )
    return x


def parse_process_response(response):
    data_dict = response

    ndvi_grid = data_dict["default.tif"].astype(float)
    return ndvi_grid.squeeze()


def save_grid_to_csv(grid: np.ndarray, path: str):
    np.savetxt(path, grid, delimiter=",")


def plot_heatmap(grid: np.ndarray, output_path: str = None):
    plt.figure(figsize=(8, 6))
    plt.imshow(
        grid,
        origin="upper",
        cmap="RdYlGn",
        interpolation="bicubic",
    )
    plt.colorbar(label="NDVI")
    plt.title("NDVI Grid (Smoothed)")

    if output_path:
        plt.savefig(output_path, dpi=300)
    else:
        plt.show()
