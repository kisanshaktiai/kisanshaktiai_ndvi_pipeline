"""Run observed water layers using the same land geometry and Sentinel-2 scenes."""
from __future__ import annotations
from typing import Optional
from raster_utils import read_band, scl_masks, resolve_geometry, measurement_field
from sentinel_search import search_s2
from processor import dedupe_acquisitions
from water_layers import build_water_layers
from logger import logger


def process_land_water_layers(land: dict, scenes: Optional[list] = None, lookback_days: int = 30,
                              scene_bands: Optional[dict] = None,
                              only_scene_ids: Optional[list] = None) -> int:
    """Observed water layers for a land.

    scene_bands: arrays the NDVI pass already read for this land, keyed by
    scene id (see processor.process_acquisition band_sink). Every band this
    stage needs - B04, B03, B08, B8A, B11, SCL - was already read there over
    the same geometry with the same reference grid, so reusing them removes
    six windowed HTTP reads per scene per land and changes no output. A scene
    missing from the cache (too large, or an error during the NDVI pass) is
    read exactly as before, so no water evidence is lost.
    """
    geom, _ = resolve_geometry(land)
    geom_meas, _, _, _ = measurement_field(geom)
    items = scenes if scenes is not None else search_s2(geom, days=lookback_days)
    items = dedupe_acquisitions(items, geom)
    # Incremental: scenes already measured on an earlier night already have
    # their water layers stored (same (tenant, land, scene, layer) key);
    # re-reading them would only rewrite identical rows.
    if only_scene_ids is not None:
        wanted = set(only_scene_ids)
        items = [it for it in items if getattr(it, "id", None) in wanted]
    cache = scene_bands or {}
    count = 0
    reused = 0
    for item in items:
        try:
            cached = cache.get(getattr(item, "id", None))
            if cached:
                src = cached["bands"]
                bands = {k: src[k] for k in ("B03", "B08", "B8A", "B11") if k in src}
                masks = cached["masks"]
                ref_transform, ref_crs = cached["ref_transform"], cached["ref_crs"]
                reused += 1
            else:
                b04, ref_transform, ref_crs, coverage = read_band(item, "B04", geom_meas)
                ref = (b04.shape, ref_transform, ref_crs, coverage)
                bands = {
                    "B03": read_band(item, "B03", geom_meas, reference=ref)[0],
                    "B08": read_band(item, "B08", geom_meas, reference=ref)[0],
                    # B8A is a native 20 m band; read/resample it onto the B04
                    # reference grid before calculating NDMI so the output pixels
                    # have one common geolocation and cannot be shifted relative
                    # to the surface-water layer.
                    "B8A": read_band(item, "B8A", geom_meas, reference=ref)[0],
                    "B11": read_band(item, "B11", geom_meas, reference=ref)[0],
                }
                scl = read_band(item, "SCL", geom_meas, reference=ref, categorical=True)[0]
                masks = scl_masks(scl, coverage=coverage)
            records = build_water_layers(
                land=land, item=item, bands=bands, masks=masks,
                geom_wgs84=geom, ref_transform=ref_transform, ref_crs=ref_crs,
            )
            count += len(records)
        except Exception as exc:
            logger.warning(
                f"water layers failed land={land['id']} scene={getattr(item, 'id', '?')}: "
                f"{type(exc).__name__}: {exc}"
            )
    if reused:
        logger.info(f"water layers land={land['id']}: reused bands for {reused}/{len(items)} scene(s)")
    return count
