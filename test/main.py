import os
from dotenv import load_dotenv
from testutils import (
    create_config,
    build_bbox,
    chunks_bbox,
    build_process_request,
    parse_process_response,
    plot_heatmap,
    save_grid_to_csv,
    NDVI,
)
from sentinelhub import SentinelHubDownloadClient

CHUNK_SIZE_M = 10
AREA_SIZE_M = 100

# US coordinates
CENTER_LAT = 43.963499161653246
CENTER_LON = -96.51123789056727
TIME_RANGE = ("2025-06-01", "2025-06-30")

OUTPUT_CSV = "ndvi_grid.csv"
OUTPUT_PLOT = "ndvi_heatmap.png"


def main():
    load_dotenv()
    client_id = os.getenv("SH_CLIENT_ID")
    client_secret = os.getenv("SH_CLIENT_SECRET")

    if not client_id or not client_secret:
        raise ValueError("Missing Sentinel Hub credentials in .env")

    config = create_config()
    bbox = build_bbox(CENTER_LAT, CENTER_LON, AREA_SIZE_M)
    # chunks = chunks_bbox(bbox, AREA_SIZE_M, 10000)
    index_strategy = NDVI()

    request = build_process_request(
        bbox=bbox,
        time_range=TIME_RANGE,
        area_size_m=AREA_SIZE_M,
        chunk_size_m=CHUNK_SIZE_M,
        index_strategy=index_strategy,
        config=config,
    )

    client = SentinelHubDownloadClient()
    response = client.download(request.download_list[0], decode_data=False)

    print(response.headers["x-processingunits-spent"])
    data = response.decode()
    print(data)

    grid = parse_process_response(data)

    save_grid_to_csv(grid, OUTPUT_CSV)
    plot_heatmap(grid, OUTPUT_PLOT)

    # from sentinelhub import SentinelHubDownloadClient

    # request = build_process_request(
    #     bbox=bbox,
    #     time_range=TIME_RANGE,
    #     area_size_m=AREA_SIZE_M,
    #     chunk_size_m=CHUNK_SIZE_M,
    #     index_strategy=index_strategy,
    #     config=config,
    # )

    # # Get download request(s)
    # download_requests = request.get_download_list()

    # # Use low-level client
    # client = SentinelHubDownloadClient(config=config)

    # print("Initializing data fetch...")

    # start_time = time.perf_counter()

    # responses = client.download(
    #     download_requests, show_progress=True, decode_data=False
    # )

    # end_time = time.perf_counter()

    # execution_time = end_time - start_time

    # print(f"API execution time: {execution_time:.3f} seconds")

    # # Now inspect headers
    # for response in responses:
    #     print(response.headers)


if __name__ == "__main__":
    main()
