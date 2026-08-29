"""
processor.py - per-acquisition field processing.

ONE ROW PER ACQUISITION. Every statistic in a row is spatial, computed over
the same buffered field on the same scene at the same instant. Temporal
analysis happens downstream over rows.

v2.2 CHANGES (forensic audit 2026-08-29)
----------------------------------------
F-1  Field footprint comes from the 10 m reference band's rasterio mask and
     is passed to scl_masks(); out-of-polygon cells can no longer be counted.
     A pixel-count plausibility gate refuses any observation whose field
     pixel count exceeds what the surveyed area can physically contain.
F-5  One physical Sentinel-2 acquisition arriving in two overlapping MGRS
     tiles (same datetime + relative orbit) is processed ONCE: the tile whose
     footprint contains the field is preferred.
F-8/F-10  Handled in raster_utils (NaN fill, no clipping of negatives).
NEW  Temporal plausibility flag: |delta NDVI| > TEMPORAL_MAX_DELTA against
     any stored optical observation within TEMPORAL_WINDOW_DAYS is recorded
     in metadata.temporal_outlier (flag, not rejection - harvest and
     flooding are real).
NEW  Per-scene exceptions are reported to the caller (scene_errors) so they
     reach ndvi_processing_logs instead of a console warning only.
"""

from typing import List, Optional, Tuple
import time
import numpy as np
from shapely.geometry import shape

from sentinel_search import search_s2, search_s1, acquisition_meta
from raster_utils import (read_band, scl_masks, apply_crop_mask,
                          buffered_field, resolve_geometry)
from indices import compute_indices, validate_index, index_statistics
from sar_vegetation import rvi_from_gamma0
from quality import assess
from config import (
    NDVI_DECIMALS, ENABLE_S1_FALLBACK, NDVI_HISTOGRAM_BINS,
    QUALITY_SATURATION_PIXELS, MICRO_LAND_ACRES, MICRO_LAND_FACTOR,
    GEOMETRY_CONFIDENCE_FACTOR, PIXEL_AREA_M2, PIXEL_COUNT_TOLERANCE,
    DEDUPE_TILE_OVERLAP, TEMPORAL_MAX_DELTA, TEMPORAL_WINDOW_DAYS,
    CLOUD_DILATION_PX,
)
from logger import logger

PIPELINE_VERSION = "v2.2"

# 10 m reference bands + the 20 m bands we resample onto them.
S2_BANDS_10M = ["B02", "B03", "B04", "B08"]
S2_BANDS_20M = ["B05", "B11"]

INDEX_COLUMNS = (
    ("NDVI",  "ndvi"),  ("SAVI",  "savi"),  ("EVI",   "evi"),
    ("NDRE",  "ndre"),  ("MCARI", "mcari"), ("NDMI",  "ndmi"),
    ("NDWI",  "ndwi"),  ("MNDWI", "mndwi_water"),
)


def _r(x, nd=NDVI_DECIMALS):
    """Round that treats 0.0 correctly and refuses non-finite values."""
    if x is None:
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    if not np.isfinite(v):
        return None
    return round(v, nd)


def max_plausible_pixels(area_m2: float) -> int:
    """Upper bound on 10 m cells a field of `area_m2` can contain (F-1 gate)."""
    if not area_m2 or area_m2 <= 0:
        return 0
    return int(np.ceil(area_m2 / PIXEL_AREA_M2 * PIXEL_COUNT_TOLERANCE)) + 2


# ---------------------------------------------------------------------------
# TILE-OVERLAP DEDUPLICATION  (F-5)
# ---------------------------------------------------------------------------
def dedupe_acquisitions(items: List, field_geom) -> List:
    """
    Collapse STAC items that are the SAME physical acquisition delivered in
    overlapping MGRS tiles. Key = (datetime, relative orbit, platform).
    Preference: the tile whose footprint contains the whole field, then the
    one with the larger intersection, then STAC order.
    """
    if not DEDUPE_TILE_OVERLAP:
        return items
    groups = {}
    order = []
    for it in items:
        p = it.properties
        key = (str(it.datetime), p.get("sat:relative_orbit"), p.get("platform"))
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(it)

    kept = []
    for key in order:
        cands = groups[key]
        if len(cands) == 1:
            kept.append(cands[0])
            continue

        def score(it):
            try:
                g = shape(it.geometry)
                if g.contains(field_geom):
                    return 2.0 + g.intersection(field_geom).area
                return g.intersection(field_geom).area
            except Exception:
                return 0.0

        best = max(cands, key=score)
        kept.append(best)
        logger.info(
            f"Tile overlap: {len(cands)} items for {key[0]} orbit {key[1]}; "
            f"using {best.id}"
        )
    return kept


# ---------------------------------------------------------------------------
# SENTINEL-2
# ---------------------------------------------------------------------------
def process_acquisition(item, geom_buffered, buffer_applied: bool,
                        land: dict, geom_conf: str = "high",
                        raw_area_m2: float = None,
                        reject_sink: Optional[list] = None,
                        error_sink: Optional[list] = None) -> Optional[dict]:
    """
    Process ONE Sentinel-2 acquisition over ONE field.
    Returns a complete row dict, or None if the acquisition is rejected.
    """
    meta = acquisition_meta(item)
    _t0 = time.time()

    try:
        # --- reference grid: B04 at 10 m, WITH its footprint -----------
        b04, ref_transform, ref_crs, footprint = read_band(item, "B04", geom_buffered)
        ref = (b04.shape, ref_transform, ref_crs, footprint)

        with_ref = {"B04": b04}
        for bk in S2_BANDS_10M:
            if bk != "B04":
                with_ref[bk], _, _, _ = read_band(item, bk, geom_buffered, reference=ref)

        for bk in S2_BANDS_20M:
            try:
                with_ref[bk], _, _, _ = read_band(item, bk, geom_buffered, reference=ref)
            except Exception as e:
                logger.debug(f"Band {bk} unavailable on {meta['scene_id']}: {e}")

        # --- SCL: nearest resampling, padded+dilated in read_band -------
        scl, _, _, _ = read_band(item, "SCL", geom_buffered, reference=ref, categorical=True)

        masks = scl_masks(scl, footprint=footprint)

        # --- F-1 PLAUSIBILITY GATE --------------------------------------
        # A footprint larger than the surveyed area can hold is impossible
        # and would mean out-of-polygon cells are being counted.
        max_px = max_plausible_pixels(raw_area_m2)
        if raw_area_m2 and masks["n_field_pixels"] > max_px:
            reason = (f"field_pixels {masks['n_field_pixels']} > plausible "
                      f"{max_px} for {raw_area_m2:.0f} m2")
            logger.error(f"REJECT optical | land={land['id']} scene={meta['scene_id']} | {reason}")
            if reject_sink is not None:
                reject_sink.append({"scene_id": meta["scene_id"],
                                    "acquisition_date": meta["acquisition_date"],
                                    "reason": reason})
            return None

        qa = assess(masks, buffer_applied,
                    area_acres=land.get("area_acres"),
                    geometry_confidence=geom_conf)

        if not qa.accepted:
            logger.info(
                f"REJECT optical | land={land['id']} scene={meta['scene_id']} "
                f"| {qa.reject_reason} "
                f"| cloud={qa.cloud_fraction:.0%} shadow={qa.shadow_fraction:.0%} "
                f"snow={qa.snow_fraction:.0%} water={qa.water_fraction:.0%} "
                f"valid_px={qa.valid_pixels}/{qa.field_pixels} "
                f"valid_frac={qa.valid_fraction:.0%} q={qa.quality_score}"
            )
            if reject_sink is not None:
                reject_sink.append({
                    "scene_id": meta["scene_id"],
                    "acquisition_date": meta["acquisition_date"],
                    "reason": qa.reject_reason,
                    "cloud_fraction": qa.cloud_fraction,
                    "shadow_fraction": qa.shadow_fraction,
                    "snow_fraction": qa.snow_fraction,
                    "water_fraction": qa.water_fraction,
                    "valid_pixels": qa.valid_pixels,
                    "field_pixels": qa.field_pixels,
                    "valid_fraction": qa.valid_fraction,
                    "quality_score": qa.quality_score,
                })
            return None

        # --- indices over crop-surface pixels only ----------------------
        masked = apply_crop_mask(with_ref, masks)
        idx = compute_indices(masked)

        row = {
            "land_id": land["id"],
            "tenant_id": land["tenant_id"],

            "scene_id": meta["scene_id"],
            "acquisition_time": meta["acquisition_time"],
            "acquisition_date": meta["acquisition_date"],
            "date": meta["acquisition_date"],
            "cloud_cover": _r(qa.cloud_fraction * 100.0, 2),
            "scene_cloud_cover": meta["scene_cloud_cover"],
            "tile_id": meta["mgrs_tile"],
            "relative_orbit": meta["relative_orbit"],
            "platform": meta["platform"],
            "processing_baseline": meta["processing_baseline"],

            "observation_source": "sentinel-2",
            "observation_type": "observed",
            "is_interpolated": False,
            "source_scene_count": 1,

            "quality_score": qa.quality_score,
            "confidence_score": qa.confidence_score,
            "confidence_level": qa.confidence_level,
            "valid_pixels": qa.valid_pixels,
            "total_pixels": qa.field_pixels,
            "coverage_percentage": _r(qa.valid_fraction * 100.0, 2),
            "shadow_fraction": qa.shadow_fraction,
            "water_fraction": qa.water_fraction,
            "snow_fraction": qa.snow_fraction,
            "saturated_fraction": qa.saturated_fraction,
            "valid_fraction": qa.valid_fraction,
            "buffer_applied": qa.buffer_applied,
            "geometry_confidence": geom_conf,

            "satellite_source": "sentinel-2",
            "collection_id": "sentinel-2-l2a",
            "processing_level": "L2A",
            "spatial_resolution": 10,
        }

        index_quality = {}
        for name, col in INDEX_COLUMNS:
            arr = idx.get(name)
            if arr is None:
                continue
            st = index_statistics(arr)
            if not st:
                continue
            ok, why = validate_index(name, st["mean"])
            index_quality[name] = {"valid": ok, "reason": why, "pixels": st["count"]}
            if not ok:
                logger.warning(f"{name} rejected ({why}, mean={st['mean']}) on "
                               f"{meta['scene_id']} land {land['id']}")
                continue

            row[f"{col}_value" if col != "mndwi_water" else "mndwi_water"] = _r(st["mean"])

            if name == "NDVI":
                # The NDVI pixel population MUST equal the crop-pixel count;
                # a shortfall means NaN bands leaked in and the row is not
                # describing the field it claims to.
                if st["count"] < qa.valid_pixels:
                    row["valid_pixels"] = st["count"]
                    row["coverage_percentage"] = _r(100.0 * st["count"] / max(qa.field_pixels, 1), 2)
                row["ndvi_spatial_min"]    = _r(st["min"])
                row["ndvi_spatial_max"]    = _r(st["max"])
                row["ndvi_spatial_std"]    = _r(st["std"])
                row["ndvi_spatial_median"] = _r(st["median"])
                row["ndvi_p10"]            = _r(st["p10"])
                row["ndvi_p90"]            = _r(st["p90"])
                row["uniformity_cv"]       = _r(st["cv"], 4)
                finite = arr[np.isfinite(arr)]
                if finite.size:
                    counts, edges = np.histogram(finite, bins=NDVI_HISTOGRAM_BINS, range=(-1.0, 1.0))
                    row["ndvi_histogram"] = {"bins": [round(float(e), 3) for e in edges],
                                             "counts": [int(c) for c in counts]}

        if "ndvi_value" not in row:
            return None

        row["processing_duration_ms"] = int((time.time() - _t0) * 1000)
        row["metadata"] = {
            "quality_breakdown": qa.to_dict(),
            "index_quality": index_quality,
            "scene_cloud_cover": meta["scene_cloud_cover"],
            "bands_available": sorted(k for k in with_ref if with_ref[k] is not None),
            "scl_accounting": {
                "cloud": qa.cloud_fraction, "shadow": qa.shadow_fraction,
                "water": qa.water_fraction, "snow": qa.snow_fraction,
                "saturated": qa.saturated_fraction,
                "unaccounted": qa.unaccounted_fraction,
            },
            "footprint": {
                "field_pixels": qa.field_pixels,
                "max_plausible_pixels": max_px,
                "raw_area_m2": round(raw_area_m2, 1) if raw_area_m2 else None,
                "cloud_dilation_px": CLOUD_DILATION_PX,
            },
            "pipeline_version": PIPELINE_VERSION,
        }
        return row

    except Exception as e:
        logger.warning(f"Acquisition {meta.get('scene_id')} failed for land {land['id']}: {e}")
        if error_sink is not None:
            error_sink.append({"scene_id": meta.get("scene_id"),
                               "acquisition_date": meta.get("acquisition_date"),
                               "error": f"{type(e).__name__}: {str(e)[:300]}"})
        return None


# ---------------------------------------------------------------------------
# TEMPORAL PLAUSIBILITY  (flag only)
# ---------------------------------------------------------------------------
def flag_temporal_outliers(rows: List[dict], history: List[dict]) -> None:
    """
    history: [{acquisition_date: 'YYYY-MM-DD', ndvi_value: float}, ...] from
    the DB (previous optical observations). Rows are compared against history
    AND against each other. Sets metadata.temporal_outlier / temporal_ref.
    """
    from datetime import date as _date
    pts = [(r["acquisition_date"], r["ndvi_value"]) for r in history
           if r.get("ndvi_value") is not None and r.get("acquisition_date")]
    pts += [(r["acquisition_date"], r["ndvi_value"]) for r in rows
            if r.get("ndvi_value") is not None]
    for r in rows:
        if r.get("ndvi_value") is None:
            continue
        d0 = _date.fromisoformat(r["acquisition_date"])
        worst = None
        for d, v in pts:
            if d == r["acquisition_date"]:
                continue
            dd = abs((_date.fromisoformat(d) - d0).days)
            if 0 < dd <= TEMPORAL_WINDOW_DAYS:
                delta = abs(float(v) - float(r["ndvi_value"]))
                if delta > TEMPORAL_MAX_DELTA and (worst is None or delta > worst[1]):
                    worst = (d, delta)
        r.setdefault("metadata", {})["temporal_outlier"] = worst is not None
        if worst:
            r["metadata"]["temporal_ref"] = {"date": worst[0], "abs_delta": round(worst[1], 4)}
            logger.warning(f"Land {r['land_id']} {r['acquisition_date']}: NDVI jump "
                           f"{worst[1]:.2f} vs {worst[0]} flagged temporal_outlier")


# ---------------------------------------------------------------------------
# PER-LAND DRIVER
# ---------------------------------------------------------------------------
def process_land(land: dict, lookback_days: int = None,
                 scenes: Optional[List] = None,
                 history: Optional[List[dict]] = None) -> Tuple[List[dict], dict]:
    """
    Returns (rows, report). rows: one per accepted acquisition.
    report: {"optical_rejects": [...], "scene_errors": [...], "items": n,
             "deduped": n, "geometry_confidence": str}
    scenes: pre-fetched STAC items (tile group). None -> per-land search.
    """
    report = {"optical_rejects": [], "scene_errors": [], "items": 0,
              "deduped": 0, "geometry_confidence": None}

    try:
        geom, geom_conf = resolve_geometry(land)
    except Exception as e:
        logger.error(f"No usable geometry for land {land['id']}: {e}")
        report["scene_errors"].append({"error": f"geometry: {e}"})
        return [], report
    report["geometry_confidence"] = geom_conf

    geom_buf, buffer_applied, raw_area_m2 = buffered_field(geom)

    items = scenes if scenes is not None else search_s2(geom, days=lookback_days)
    report["items"] = len(items)
    items = dedupe_acquisitions(items, geom)
    report["deduped"] = report["items"] - len(items)

    rows = []
    for item in items:
        r = process_acquisition(item, geom_buf, buffer_applied, land, geom_conf,
                                raw_area_m2=raw_area_m2,
                                reject_sink=report["optical_rejects"],
                                error_sink=report["scene_errors"])
        if r:
            r["field_area_m2"] = round(raw_area_m2, 1)
            rows.append(r)

    if rows:
        flag_temporal_outliers(rows, history or [])
        logger.info(
            f"Land {land['id']}: {len(rows)}/{len(items)} acquisitions accepted "
            f"(dates {rows[-1]['acquisition_date']} .. {rows[0]['acquisition_date']})"
        )
        return rows, report

    if not ENABLE_S1_FALLBACK:
        return [], report

    rejects = report["optical_rejects"]
    if rejects:
        reasons = {}
        for r in rejects:
            key = (r.get("reason") or "unknown").split()[0]
            reasons[key] = reasons.get(key, 0) + 1
        cf = [r["cloud_fraction"] for r in rejects if r.get("cloud_fraction") is not None]
        logger.info(f"Land {land['id']}: optical 0/{len(items)} accepted | reasons={reasons}"
                    + (f" | mean_field_cloud={sum(cf)/len(cf):.0%}" if cf else ""))
    logger.info(f"Land {land['id']}: no usable optical data, trying Sentinel-1")
    s1 = _process_s1(land, geom_buf, buffer_applied, geom_conf, raw_area_m2, report)
    for row in s1:
        row.setdefault("metadata", {})["optical_rejects"] = rejects[:6]
    return s1, report


# ---------------------------------------------------------------------------
# SENTINEL-1 FALLBACK
# ---------------------------------------------------------------------------
def _process_s1(land: dict, geom_buffered, buffer_applied: bool,
                geom_conf: str, raw_area_m2: float, report: dict) -> List[dict]:
    pairs = search_s1(geom_buffered)
    if not pairs:
        return []

    collection, item = pairs[0]
    meta = acquisition_meta(item)
    _t0 = time.time()

    try:
        assets = {k.lower(): k for k in item.assets}
        vv, ref_transform, ref_crs, footprint = read_band(item, assets["vv"], geom_buffered)
        ref = (vv.shape, ref_transform, ref_crs, footprint)
        vh, _, _, _ = read_band(item, assets["vh"], geom_buffered, reference=ref)

        max_px = max_plausible_pixels(raw_area_m2)
        n_fp = int(np.count_nonzero(footprint))
        if raw_area_m2 and n_fp > max_px:
            logger.error(f"REJECT radar | land={land['id']} | footprint {n_fp} > plausible {max_px}")
            return []

        res = rvi_from_gamma0(vv, vh)
        _px = res.get("valid_pixels") or 0
        _s1_quality = 0.50 * min(_px / QUALITY_SATURATION_PIXELS, 1.0)
        _area = land.get("area_acres")
        _s1_confidence = _s1_quality * 0.70
        if _area is not None and _area < MICRO_LAND_ACRES:
            _s1_confidence *= MICRO_LAND_FACTOR
        _s1_confidence *= GEOMETRY_CONFIDENCE_FACTOR.get(geom_conf, 0.5)
        _s1_quality = round(_s1_quality, 3)
        # strictly below quality by a float4-safe margin (F-2 guard)
        _s1_confidence = round(min(_s1_confidence, _s1_quality - 1e-3), 3)
        _s1_confidence = max(_s1_confidence, 0.0)

        if not res["accepted"]:
            logger.info(f"REJECT radar | land={land['id']} | {res['reject_reason']}")
            return []

        return [{
            "land_id": land["id"],
            "tenant_id": land["tenant_id"],
            "scene_id": meta["scene_id"],
            "acquisition_time": meta["acquisition_time"],
            "acquisition_date": meta["acquisition_date"],
            "date": meta["acquisition_date"],
            "ndvi_value": None,
            "rvi_value": res["rvi_mean"],
            "rvi_std": res["rvi_std"],
            "cross_ratio_db": res["cross_ratio_db"],
            "observation_source": "sentinel-1",
            "observation_type": "observed",
            "is_interpolated": False,
            "source_scene_count": 1,
            "satellite_source": "sentinel-1",
            "collection_id": collection,
            "processing_level": "RTC" if "rtc" in collection else "GRD",
            "spatial_resolution": 10,
            "relative_orbit": meta["relative_orbit"],
            "platform": meta["platform"],
            "valid_pixels": res["valid_pixels"],
            "total_pixels": n_fp,
            "field_area_m2": round(raw_area_m2, 1),
            "quality_score": _s1_quality,
            "confidence_score": _s1_confidence,
            "confidence_level": ("high" if _s1_confidence >= 0.40
                                 else "medium" if _s1_confidence >= 0.25 else "low"),
            "geometry_confidence": geom_conf,
            "buffer_applied": buffer_applied,
            "processing_duration_ms": int((time.time() - _t0) * 1000),
            "metadata": {
                "note": "optical unavailable (cloud); radar vegetation proxy",
                "index": "RVI = 4*VH/(VV+VH), linear gamma0, range [0,2]",
                "vv_db_mean": res.get("vv_db_mean"),
                "vh_db_mean": res.get("vh_db_mean"),
                "pipeline_version": PIPELINE_VERSION,
            },
        }]
    except Exception as e:
        logger.warning(f"S1 processing failed for land {land['id']}: {e}")
        report["scene_errors"].append({"scene_id": meta.get("scene_id"),
                                       "error": f"S1 {type(e).__name__}: {str(e)[:300]}"})
        return []
