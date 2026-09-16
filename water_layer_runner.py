"""Run observed water layers using the same land geometry and Sentinel-2 scenes."""
from __future__ import annotations
from typing import Optional
from raster_utils import read_band, scl_masks, resolve_geometry, measurement_field
from sentinel_search import search_s2
from processor import dedupe_acquisitions
from water_layers import build_water_layers
from logger import logger


def process_land_water_layers(land: dict, scenes: Optional[list] = None, lookback_days: int = 30) -> int:
    geom, _ = resolve_geometry(land)
    geom_meas, _, _, _ = measurement_field(geom)
    items = scenes if scenes is not None else search_s2(geom, days=lookback_days)
    items = dedupe_acquisitions(items, geom)
    count = 0
    for item in items:
        try:
            b04, ref_transform, ref_crs, coverage = read_band(item, "B04", geom_meas)
            ref = (b04.shape, ref_transform, ref_crs, coverage)
            bands = {"B03": read_band(item, "B03", geom_meas, reference=ref)[0],
                     "B08": read_band(item, "B08", geom_meas, reference=ref)[0],
                     "B11": read_band(item, "B11", geom_meas, reference=ref)[0]}
            scl = read_band(item, "SCL", geom_meas, reference=ref, categorical=True)[0]
            masks = scl_masks(scl, coverage=coverage)
            records = build_water_layers(land=land, item=item, bands=bands, masks=masks,
                                         geom_wgs84=geom, ref_transform=ref_transform, ref_crs=ref_crs)
            count += len(records)
        except Exception as exc:
            logger.warning(f"water layers failed land={land['id']} scene={getattr(item, 'id', '?')}: {type(exc).__name__}: {exc}")
    return count
