"""
processor.py - per-acquisition field processing.

THE CENTRAL ARCHITECTURAL CHANGE OF v2.

v1 collapsed every scene in a 15-day window into ONE row (mean 3.57 scenes),
stamped it with date.today(), and mixed temporal and spatial statistics in
the same record:

    ndvi_value = nanmean(ndvi_series)      # TEMPORAL, across scenes
    min_ndvi   = nanmin(ndvi_series)       # TEMPORAL, across scenes
    ndvi_std   = nanstd(ndvi_raster)       # SPATIAL, one arbitrary scene
    median     = nanmedian(ndvi_raster)    # SPATIAL, one arbitrary scene

Proof of incoherence from production: 53.8% of rows have median_ndvi outside
[min_ndvi, max_ndvi] - mathematically impossible if they shared a frame. And
avg(max-min) = 0.0538 is SMALLER than avg(ndvi_std) = 0.0734: the "range"
is narrower than the standard deviation.

v2 emits ONE ROW PER ACQUISITION. Every statistic in a row is spatial,
computed over the same buffered field on the same scene at the same instant.
Temporal analysis is done downstream over rows, where it belongs.
"""

from typing import List, Optional
import time
import numpy as np
from shapely.geometry import shape

from sentinel_search import search_s2, search_s1, acquisition_meta
from raster_utils import read_band, scl_masks, apply_crop_mask, buffered_field, resolve_geometry
from indices import compute_indices, validate_index, index_statistics
from sar_vegetation import rvi_from_gamma0
from quality import assess
from config import (
    NDVI_DECIMALS, ENABLE_S1_FALLBACK, SCENE_WORKERS, NDVI_HISTOGRAM_BINS,
    QUALITY_SATURATION_PIXELS, MICRO_LAND_ACRES, MICRO_LAND_FACTOR,
    GEOMETRY_CONFIDENCE_FACTOR,
)
from logger import logger

# 10 m reference bands + the 20 m bands we resample onto them.
S2_BANDS_10M = ["B02", "B03", "B04", "B08"]
S2_BANDS_20M = ["B05", "B11"]


def _r(x, nd=NDVI_DECIMALS):
    """Round that treats 0.0 correctly.

    v1 used `round(x, n) if result.get(x) else None`, which is falsy for 0.0
    and silently NULLed a genuine ndvi_std of exactly 0 (uniform field) or a
    median of 0 (bare soil). (P-10)
    """
    if x is None:
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    if not np.isfinite(v):
        return None
    return round(v, nd)


def process_acquisition(item, geom_buffered, buffer_applied: bool,
                        land: dict, geom_conf: str = "high",
                        reject_sink: Optional[list] = None) -> Optional[dict]:
    """
    Process ONE Sentinel-2 acquisition over ONE field.
    Returns a complete row dict, or None if the acquisition is rejected.
    """
    meta = acquisition_meta(item)
    _t0 = time.time()

    try:
        # --- reference grid: B04 at 10 m -------------------------------
        # Reference grid: B04 at 10 m. The CRS MUST be carried through -
        # passing None here made reproject() raise "Missing dst_crs" on every
        # band of every scene, which surfaced as a total data outage rather
        # than the one-line contract bug it was.
        b04, ref_transform, ref_crs = read_band(item, "B04", geom_buffered)
        ref = (b04.shape, ref_transform, ref_crs)

        with_ref = {}
        for bk in S2_BANDS_10M:
            if bk == "B04":
                with_ref[bk] = b04
            else:
                with_ref[bk], _, _ = read_band(item, bk, geom_buffered, reference=ref)

        for bk in S2_BANDS_20M:
            try:
                with_ref[bk], _, _ = read_band(item, bk, geom_buffered, reference=ref)
            except Exception as e:
                logger.debug(f"Band {bk} unavailable on {meta['scene_id']}: {e}")

        # --- SCL: NEAREST resampling (P-04 fix) ------------------------
        scl, _, _ = read_band(item, "SCL", geom_buffered, reference=ref, categorical=True)

        masks = scl_masks(scl)
        qa = assess(
            masks,
            buffer_applied,
            area_acres=land.get("area_acres"),
            geometry_confidence=geom_conf,
        )

        if not qa.accepted:
            # INFO, not DEBUG. The first live run rejected 100% of optical
            # acquisitions on all 29 lands and the reason was invisible at
            # LOG_LEVEL=INFO - indistinguishable from a bug. A rejection
            # without a stated reason is the exact failure mode this pipeline
            # exists to remove.
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

        # --- indices over crop-surface pixels only ---------------------
        masked = apply_crop_mask(with_ref, masks)
        idx = compute_indices(masked)

        row = {
            "land_id": land["id"],
            "tenant_id": land["tenant_id"],

            # ---- TRUE PROVENANCE (all NULL in v1) --------------------
            "scene_id": meta["scene_id"],
            "acquisition_time": meta["acquisition_time"],
            "acquisition_date": meta["acquisition_date"],
            "date": meta["acquisition_date"],     # legacy col = TRUE date now
            "cloud_cover": _r(qa.cloud_fraction * 100.0, 2),
            "scene_cloud_cover": meta["scene_cloud_cover"],
            "tile_id": meta["mgrs_tile"],
            "relative_orbit": meta["relative_orbit"],
            "platform": meta["platform"],
            "processing_baseline": meta["processing_baseline"],

            # ---- OBSERVATION SEMANTICS (new, non-negotiable) ---------
            "observation_source": "sentinel-2",
            "observation_type": "observed",       # NEVER interpolated
            "is_interpolated": False,

            # ---- QUALITY (NULL in 99.84% of v1 rows) -----------------
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

        # ---- SPATIAL statistics, all from THIS acquisition -----------
        # Every statistic here is SPATIAL: computed over the valid pixels of
        # one field on one acquisition. No temporal aggregation happens
        # anywhere in this pipeline. v1 mixed the two frames in one row and
        # 53.8% of its rows were mathematically impossible as a result.
        #
        # DETERMINISM: fixed iteration order, nan-aware numpy reductions,
        # no sampling. Identical imagery yields bit-identical output, which
        # the Decision Brain requires.
        INDEX_COLUMNS = (
            ("NDVI",  "ndvi"),  ("SAVI",  "savi"),  ("EVI",   "evi"),
            ("NDRE",  "ndre"),  ("MCARI", "mcari"), ("NDMI",  "ndmi"),
            ("NDWI",  "ndwi"),  ("MNDWI", "mndwi_water"),
        )
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
                logger.warning(
                    f"{name} rejected ({why}, mean={st['mean']}) on "
                    f"{meta['scene_id']} land {land['id']}"
                )
                continue

            row[f"{col}_value" if col != "mndwi_water" else "mndwi_water"] = _r(st["mean"])

            # NDVI carries the full distribution: it drives stage-relative
            # interpretation and within-field heterogeneity downstream.
            if name == "NDVI":
                row["ndvi_spatial_min"]    = _r(st["min"])
                row["ndvi_spatial_max"]    = _r(st["max"])
                row["ndvi_spatial_std"]    = _r(st["std"])
                row["ndvi_spatial_median"] = _r(st["median"])
                row["ndvi_p10"]            = _r(st["p10"])
                row["ndvi_p90"]            = _r(st["p90"])
                # Coefficient of variation: THE differential splitter.
                #   uniform low -> whole-field cause (nutrient, water)
                #   patchy  low -> localised cause (pest, disease, soil)
                row["uniformity_cv"]       = _r(st["cv"], 4)

                # ---- PER-FIELD HISTOGRAM ----------------------------
                # Adopted from the retired engine repo, which stored a
                # 20-bin distribution rather than summary stats alone.
                # ~200 bytes, and percentiles/uniformity/bimodality stay
                # recomputable later without touching imagery again.
                #
                # DIFFERENCE THAT MATTERS: that repo built ONE histogram
                # per 12,060 km2 MGRS tile and weighted it to fields by
                # overlap ratio. A 5-acre field is 0.00017% of such a
                # tile, so the histogram described a river basin. This one
                # covers the BUFFERED FIELD only.
                finite = arr[np.isfinite(arr)]
                if finite.size:
                    counts, edges = np.histogram(
                        finite, bins=NDVI_HISTOGRAM_BINS, range=(-1.0, 1.0))
                    row["ndvi_histogram"] = {
                        "bins": [round(float(e), 3) for e in edges],
                        "counts": [int(c) for c in counts],
                    }

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
            "pipeline_version": "v2.1",
        }
        return row

    except Exception as e:
        logger.warning(
            f"Acquisition {meta.get('scene_id')} failed for land {land['id']}: {e}"
        )
        return None


def process_land(land: dict, lookback_days: int = None,
                 scenes: Optional[List] = None) -> List[dict]:
    """
    scenes: pre-fetched STAC items from tile_grouping.scenes_for_group().
    Passing them avoids one STAC search per land - the ~2000x scaling win
    the retired engine repo was reaching for. When None, falls back to a
    per-land search.
    """
    """
    Returns a LIST of rows - one per accepted acquisition.
    v1 returned at most one row per land per run.
    """
    # Geometry with an honest confidence label, adopted from the retired
    # engine repo (land_geometry.resolve_land_geometry). It degrades to a
    # centroid buffer rather than refusing - but labels that 'low' so the
    # quality score can discount it instead of pretending it is a survey.
    try:
        geom, geom_conf = resolve_geometry(land)
    except Exception as e:
        logger.error(f"No usable geometry for land {land['id']}: {e}")
        return []

    geom_buf, buffer_applied, area_m2 = buffered_field(geom)

    items = scenes if scenes is not None else search_s2(geom, days=lookback_days)
    rows = []
    rejects: List[dict] = []
    for item in items:
        r = process_acquisition(item, geom_buf, buffer_applied, land, geom_conf,
                                reject_sink=rejects)
        if r:
            r["field_area_m2"] = round(area_m2, 1)
            rows.append(r)

    if rows:
        logger.info(
            f"Land {land['id']}: {len(rows)}/{len(items)} acquisitions accepted "
            f"(dates {rows[-1]['acquisition_date']} .. {rows[0]['acquisition_date']})"
        )
        return rows

    # ---- OPTICAL FAILED -> SENTINEL-1 FALLBACK ------------------------
    # This is what keeps the platform alive through the monsoon instead of
    # going 100% blind as it did in July-August 2026.
    if not ENABLE_S1_FALLBACK:
        return []

    # Aggregate WHY optical failed, so a monsoon rejection is instantly
    # distinguishable from a processing bug.
    if rejects:
        reasons = {}
        for r in rejects:
            key = (r["reason"] or "unknown").split()[0]
            reasons[key] = reasons.get(key, 0) + 1
        logger.info(
            f"Land {land['id']}: optical 0/{len(items)} accepted | "
            f"reasons={reasons} | "
            f"mean_field_cloud={sum(r['cloud_fraction'] for r in rejects)/len(rejects):.0%}"
        )
    logger.info(f"Land {land['id']}: no usable optical data, trying Sentinel-1")
    s1 = _process_s1(land, geom_buf, buffer_applied, geom_conf)
    for row in s1:
        row.setdefault("metadata", {})["optical_rejects"] = rejects[:6]
    return s1


def _process_s1(land: dict, geom_buffered, buffer_applied: bool,
                geom_conf: str = "high") -> List[dict]:
    pairs = search_s1(geom_buffered)
    if not pairs:
        return []

    collection, item = pairs[0]
    meta = acquisition_meta(item)
    _t0 = time.time()

    try:
        assets = {k.lower(): k for k in item.assets}
        vv, ref_transform, ref_crs = read_band(item, assets["vv"], geom_buffered)
        ref = (vv.shape, ref_transform, ref_crs)
        vh, _, _ = read_band(item, assets["vh"], geom_buffered, reference=ref)

        res = rvi_from_gamma0(vv, vh)

        # Pixel-support factor, mirroring quality.assess() for optical.
        # Saturates at QUALITY_SATURATION_PIXELS so a 2000-pixel field is not
        # rewarded indefinitely, but a 15-pixel field is honestly discounted.
        _px = res.get("valid_pixels") or 0
        _s1_quality = 0.50 * min(_px / QUALITY_SATURATION_PIXELS, 1.0)

        # Decision confidence discounts further for micro-land and for
        # geometry provenance - the same factors optical rows carry.
        _area = land.get("area_acres")
        _s1_confidence = _s1_quality * 0.70
        if _area is not None and _area < MICRO_LAND_ACRES:
            _s1_confidence *= MICRO_LAND_FACTOR
        _s1_confidence *= GEOMETRY_CONFIDENCE_FACTOR.get(geom_conf, 0.5)
        _s1_confidence = min(_s1_confidence, _s1_quality)   # DB CHECK invariant

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

            # NDVI IS NULL HERE - BY DESIGN.
            # A radar index is not an optical measurement and must never be
            # written into ndvi_value.
            "ndvi_value": None,
            "rvi_value": res["rvi_mean"],
            "rvi_std": res["rvi_std"],
            "cross_ratio_db": res["cross_ratio_db"],

            "observation_source": "sentinel-1",
            "observation_type": "observed",
            "is_interpolated": False,
            "satellite_source": "sentinel-1",
            "collection_id": collection,
            "processing_level": "RTC" if "rtc" in collection else "GRD",
            "spatial_resolution": 10,

            "valid_pixels": res["valid_pixels"],
            # Radar is a structural proxy, not an optical measurement, so
            # quality is CAPPED at 0.50 - but it must still VARY with pixel
            # support. The 2026-08-07 run wrote 0.50/0.35 to all 28 rows, so a
            # 15-pixel observation and a 2211-pixel observation were
            # indistinguishable: exactly the defect v1 was faulted for.
            "quality_score": _r(_s1_quality, 3),
            "confidence_score": _r(_s1_confidence, 3),
            "confidence_level": ("high" if _s1_confidence >= 0.40
                                 else "medium" if _s1_confidence >= 0.25
                                 else "low"),
            "geometry_confidence": geom_conf,
            "buffer_applied": buffer_applied,
            "metadata": {
                "note": "optical unavailable (cloud); radar vegetation proxy",
                "index": "RVI = 4*VH/(VV+VH)",
            },
        }]
    except Exception as e:
        logger.warning(f"S1 processing failed for land {land['id']}: {e}")
        return []