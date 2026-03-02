from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Literal


AllowedMetric = Literal[
    "NDVI",
    "PSRI",
    "NDMI",
    "NDRE",
    "NDWI",
    "TRUE_COLOR",
]


@dataclass(frozen=True)
class JobPayload:
    geojson: List[Dict[str, Any]]
    time: str
    webhook_url: Optional[str]
    resolution: int
    metrics: List[AllowedMetric]
    generate_tiff: bool = False


@dataclass(frozen=True)
class JobRow:
    job_id: str
    status: str
    created_at: str
    payload: str
