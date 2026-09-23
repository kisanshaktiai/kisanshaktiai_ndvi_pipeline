"""
processor.py - per-acquisition field processing.

ONE ROW PER ACQUISITION. Every statistic in a row is spatial, computed over
the same buffered field on the same scene at the same instant. Temporal
analysis happens downstream over rows.

v3 CHANGE (smallholder evidence audit)
--------------------------------------
The measurement primitive is no longer "a pixel". Every cell contributes
the exact fraction of it that lies inside the farmer's polygon, so a
10-guntha (~1012 m2) field yields an area-true statistic with an explicit
effective pixel count (EPC ~= area/100), a purity, a spatial standard
error and an evidence tier - instead of an unweighted mean over whatever
whole cells the mask happened to select.

The old area-based plausibility gate is retired: with coverage weighting
EPC*100 m2 IS the measured area, so the F-1 over-count it was guarding
against is now impossible by construction. What remains is a cheap
identity assertion (coverage_area_error) that would catch a regression.

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

from typing import List, Optional, Tuple, Dict
import time
import numpy as np
from shapely.geometry import shape

from sentinel_search import search_s2, search_s1, acquisition_meta
from raster_utils import (read_band, scl_masks, apply_crop_mask,
                          measurement_field, resolve_geometry)
from indices import (compute_indices, validate_index, index_statistics,
                     weighted_index_statistics, weighted_histogram)
from sar_vegetation import rvi_from_gamma0
from quality import assess, evidence_tier
from raster_io import render_ndvi_png, storage_path, upload_png
from tile_reader import subset_band, geometry_fingerprint
from resource_budget import RASTER_CACHE, COUNTERS
from config import (
    FIELD_BUFFER_M, NDVI_DECIMALS, ENABLE_S1_FALLBACK, NDVI_HISTOGRAM_BINS,
    QUALITY_SATURATION_PIXELS, MICRO_LAND_ACRES, MICRO_LAND_FACTOR,
    GEOMETRY_CONFIDENCE_FACTOR, PIXEL_AREA_M2, PIXEL_COUNT_TOLERANCE,
    DEDUPE_TILE_OVERLAP, TEMPORAL_MAX_DELTA, TEMPORAL_WINDOW_DAYS,
    CLOUD_DILATION_PX, SPATIAL_STAT_METHOD, MIN_EPC, EPC_SATURATION,
    ENABLE_NDVI_IMAGES,
)
from logger import logger

PIPELINE_VERSION = "v3.1"

from config import NDVI_IMAGE_BUCKET


def _supabase():
    """
    Lazy handle. Imported inside the call so this module stays importable
    (and unit-testable) without Supabase credentials in the environment.
    """
    from db import supabase
    return supabase

# 10 m reference bands + the 20 m bands we resample onto them.
# Upper bound on the per-scene band arrays handed to later stages (bytes).
# A 56-acre holding at 10 m is ~0.4 MB per band; 32 MB leaves headroom for the
# largest realistic parcel while keeping 10 parallel workers well inside the
# runner's memory.
BAND_CACHE_MAX_BYTES = 32 * 1024 * 1024   # one scene's arrays; see also
# resource_budget.RASTER_CACHE, the process-wide ceiling across all workers.

S2_BANDS_10M = ["B02", "B03", "B04", "B08"]
S2_BANDS_20M = ["B05", "B8A", "B11"]

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


def coverage_area_error(epc_total: float, measured_area_m2: float) -> float:
    """
    Relative error of the identity  EPC * 100 m2 == measured polygon area.

    With exact fractional coverage this is 0 to floating point, unless the
    polygon runs off the edge of the scene window (legitimate, and worth
    knowing) or the coverage computation fell back to binary. It replaces
    the v2.2 area/1.25 heuristic: that guard existed only because whole-cell
    counting could exceed the field, which coverage weighting makes
    impossible.
    """
    if not measured_area_m2 or measured_area_m2 <= 0:
        return 0.0
    return abs(epc_total * 100.0 - measured_area_m2) / measured_area_m2


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
def process_acquisition(item, geom_measured, buffer_applied: bool,
                        land: dict, geom_conf: str = "high",
                        raw_area_m2: float = None,
                        reject_sink: Optional[list] = None,
                        error_sink: Optional[list] = None,
                        measured_area_m2: float = None,
                        geom_wgs84=None,
                        band_sink: Optional[dict] = None,
                        block=None) -> Optional[dict]:
    """
    Process ONE Sentinel-2 acquisition over ONE field.
    Returns a complete row dict, or None if the acquisition is rejected.
    """
    meta = acquisition_meta(item)
    _t0 = time.time()

    try:
        # --- band access: from the tile block when this scene was read for
        # the whole block, else a per-parcel read. tile_reader.subset_band
        # reproduces read_band exactly (tests/test_tile_reader.py asserts
        # array, coverage, transform and dilated SCL equality), so values are
        # identical either way and any block failure falls back silently.
        def _band(bk, reference=None, categorical=False):
            if block is not None:
                try:
                    got = subset_band(block, bk, geom_measured, reference=reference,
                                      categorical=categorical)
                except Exception as exc:
                    # A block-slicing failure must never become a lost
                    # observation: record why, then take the canonical path.
                    COUNTERS.bump("block_subset_errors")
                    logger.warning(f"block subset failed band={bk} scene={meta.get('scene_id')}: "
                                   f"{type(exc).__name__}: {exc} - falling back to per-parcel read")
                    got = None
                if got is not None:
                    return got
                COUNTERS.bump("block_fallback_reads")
            # read_band counts itself and classifies failures (raster_utils).
            return read_band(item, bk, geom_measured, reference=reference,
                             categorical=categorical)

        # --- reference grid: B04 at 10 m, WITH exact coverage -----------
        b04, ref_transform, ref_crs, coverage = _band("B04")
        ref = (b04.shape, ref_transform, ref_crs, coverage)

        with_ref = {"B04": b04}
        for bk in S2_BANDS_10M:
            if bk != "B04":
                with_ref[bk], _, _, _ = _band(bk, reference=ref)

        for bk in S2_BANDS_20M:
            try:
                with_ref[bk], _, _, _ = _band(bk, reference=ref)
            except Exception as e:
                logger.debug(f"Band {bk} unavailable on {meta['scene_id']}: {e}")

        # --- SCL: nearest resampling, padded+dilated --------------------
        scl, _, _, _ = _band("SCL", reference=ref, categorical=True)

        masks = scl_masks(scl, coverage=coverage)

        # --- SHARE THE BANDS ALREADY PAID FOR ----------------------------
        # Every band above is a windowed HTTP read against Planetary Computer.
        # The water-layer stage needs exactly these same bands over exactly
        # this window, and used to read all six again per scene. Hand the
        # arrays over instead. This happens for EVERY evaluated scene, not
        # only accepted ones, so no water evidence is lost when NDVI rejects
        # a scene (e.g. for cloud) that still shows open water in the clear
        # part of the field. Bounded by BAND_CACHE_MAX_BYTES so a very large
        # holding cannot exhaust the runner's memory; above that the water
        # stage falls back to reading, exactly as before.
        if band_sink is not None and meta.get("scene_id"):
            try:
                nbytes = sum(int(getattr(a, "nbytes", 0)) for a in with_ref.values())
                # Two ceilings: this scene's own size, and the PROCESS-WIDE budget
                # shared by every worker. The second one is what makes the memory
                # bound real rather than theoretical (release audit, 2026-09-22):
                # once the budget is spent, nothing is cached and the water stage
                # reads exactly as it did before caching existed.
                if nbytes > BAND_CACHE_MAX_BYTES:
                    logger.debug(f"band cache skipped for {meta['scene_id']}: {nbytes} bytes > per-scene cap")
                    COUNTERS.bump("band_cache_skipped_scene_cap")
                elif not RASTER_CACHE.acquire(nbytes):
                    logger.info(f"band cache skipped for {meta['scene_id']}: process raster budget full")
                    COUNTERS.bump("band_cache_skipped_budget")
                else:
                    band_sink[meta["scene_id"]] = {
                        "bands": with_ref, "masks": masks, "coverage": coverage,
                        "ref_transform": ref_transform, "ref_crs": ref_crs,
                        "geom_measured": geom_measured, "_nbytes": nbytes,
                    }
                    COUNTERS.bump("band_cache_stored")
            except Exception as e:
                logger.debug(f"band cache skipped: {type(e).__name__}: {e}")

        # --- AREA IDENTITY CHECK ----------------------------------------
        # EPC * 100 m2 must equal the measured polygon area. A large error
        # means the field runs off the scene window or coverage fell back to
        # binary - both are worth recording, neither is a silent failure.
        area_err = coverage_area_error(masks["epc_total"], measured_area_m2)
        if area_err > 0.10:
            logger.warning(
                f"land={land['id']} scene={meta['scene_id']}: coverage area "
                f"error {area_err:.1%} (EPC {masks['epc_total']:.2f} vs "
                f"{measured_area_m2:.0f} m2) - partial scene coverage?")

        # --- indices over crop-surface pixels, coverage-weighted ---------
        masked = apply_crop_mask(with_ref, masks)
        idx = compute_indices(masked)
        crop_w = np.where(masks["crop"], masks["coverage"], 0.0)
        ndvi_stats = weighted_index_statistics(idx.get("NDVI"), crop_w) if idx.get("NDVI") is not None else None

        qa = assess(masks, buffer_applied,
                    area_acres=land.get("area_acres"),
                    geometry_confidence=geom_conf,
                    stats=ndvi_stats)

        if not qa.accepted:
            logger.info(
                f"REJECT optical | land={land['id']} scene={meta['scene_id']} "
                f"| {qa.reject_reason} | EPC={qa.epc_valid:.2f}/{qa.epc_total:.2f} "
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
                    "epc_valid": qa.epc_valid,
                    "purity": qa.purity,
                })
            return None

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
            st = ndvi_stats if name == "NDVI" else weighted_index_statistics(arr, crop_w)
            if not st:
                continue
            ok, why = validate_index(name, st["mean"])
            index_quality[name] = {"valid": ok, "reason": why,
                                   "epc": st["epc"], "cells": st["n_cells"]}
            if not ok:
                logger.warning(f"{name} rejected ({why}, mean={st['mean']}) on "
                               f"{meta['scene_id']} land {land['id']}")
                continue

            row[f"{col}_value" if col != "mndwi_water" else "mndwi_water"] = _r(st["mean"])

            if name == "NDVI":
                row["valid_pixels"] = st["n_cells"]
                row["coverage_percentage"] = _r(qa.valid_fraction * 100.0, 2)
                row["ndvi_spatial_min"]    = _r(st["min"])
                row["ndvi_spatial_max"]    = _r(st["max"])
                row["ndvi_spatial_std"]    = _r(st["std"])
                row["ndvi_spatial_median"] = _r(st["median"])
                row["ndvi_p10"]            = _r(st["p10"])
                row["ndvi_p90"]            = _r(st["p90"])
                row["uniformity_cv"]       = _r(st["cv"], 4) if st["cv"] is not None else None
                hist = weighted_histogram(arr, crop_w, NDVI_HISTOGRAM_BINS)
                if hist:
                    row["ndvi_histogram"] = hist

        if "ndvi_value" not in row:
            return None

        row["processing_duration_ms"] = int((time.time() - _t0) * 1000)

        # Calibration provenance is part of the measurement contract.
        from production_intelligence import calibration_provenance
        row.setdefault("metadata", {})
        row["metadata"]["calibration"] = {
            bk: calibration_provenance(item, bk)
            for bk in ("B02", "B03", "B04", "B05", "B8A", "B08", "B11")
            if bk in item.assets
        }

        # ---- EVIDENCE BLOCK -------------------------------------------
        # Written to metadata ALWAYS (jsonb, no migration needed) and to
        # real columns only where they exist (db.py filters unknown keys).
        from production_intelligence import spatial_effective_sample_size
        spatial_support = spatial_effective_sample_size(idx.get("NDVI"), crop_w)

        evidence = {
            "spatial_stat_method": SPATIAL_STAT_METHOD,
            "effective_pixel_count": ndvi_stats["epc"],
            "effective_pixel_count_total": round(masks["epc_total"], 4),
            "raw_valid_cell_count": ndvi_stats["n_cells"],
            "coverage_weighted_purity": ndvi_stats["purity"],
            "interior_share": ndvi_stats["interior_share"],
            "boundary_contamination_fraction": ndvi_stats["boundary_share"],
            "valid_weighted_fraction": qa.valid_fraction,
            "cloud_weighted_fraction": qa.cloud_fraction,
            "n_eff_kish": ndvi_stats["n_eff"],
            "n_eff_spatial": spatial_support.get("n_eff_spatial"),
            "spatial_se": spatial_support.get("spatial_se"),
            "spatial_support_method": spatial_support.get("method"),
            "ndvi_spatial_se": spatial_support.get("spatial_se") or ndvi_stats["se"],
            "ndvi_lower_95_spatial": _r(ndvi_stats["mean"] - 1.96 * (ndvi_stats["se"] or 0.0)),
            "ndvi_upper_95_spatial": _r(ndvi_stats["mean"] + 1.96 * (ndvi_stats["se"] or 0.0)),
            "uncertainty_scope": ("spatial sampling only; excludes sensor, "
                                  "atmospheric-correction, geolocation and "
                                  "boundary-delineation error"),
            "measurement_status": qa.measurement_status,
            "evidence_confidence": qa.evidence_confidence,
            "measured_area_m2": round(measured_area_m2, 1) if measured_area_m2 else None,
            "raw_area_m2": round(raw_area_m2, 1) if raw_area_m2 else None,
            "erosion_applied_m": FIELD_BUFFER_M if buffer_applied else 0.0,
            "coverage_area_error": round(area_err, 4),
            "observed_or_predicted": "observed",
        }
        row["effective_pixel_count"] = ndvi_stats["epc"]
        row["coverage_weighted_purity"] = ndvi_stats["purity"]
        row["boundary_contamination_fraction"] = ndvi_stats["boundary_share"]
        row["ndvi_spatial_se"] = ndvi_stats["se"]
        row["evidence_confidence"] = qa.evidence_confidence
        row["measurement_status"] = qa.measurement_status
        row["spatial_stat_method"] = SPATIAL_STAT_METHOD

        # ---- FIELD IMAGE ----------------------------------------------
        # Rendered from the SAME array the statistics came from, so the
        # picture and the number can never describe different things -
        # the mismatch the first forensic audit found when an August
        # metric sat beside a June PNG.
        image_meta = None
        if ENABLE_NDVI_IMAGES and geom_wgs84 is not None:
            try:
                rendered = render_ndvi_png(
                    ndvi=idx.get("NDVI"),
                    visible=crop_w,
                    src_transform=ref_transform,
                    src_crs=ref_crs,
                    geom_wgs84=geom_wgs84,
                )
                if rendered:
                    png, image_meta = rendered
                    path = storage_path(land["tenant_id"], land["id"],
                                        meta["acquisition_date"], meta["scene_id"])
                    stored = upload_png(_supabase(), path, png)
                    if stored:
                        row["image_url"] = stored          # PATH, not a URL
                        image_meta["storage_path"] = stored
                        image_meta["bucket"] = NDVI_IMAGE_BUCKET
                    else:
                        image_meta["upload_failed"] = True
            except Exception as e:
                logger.warning(f"NDVI image step failed for land {land['id']} "
                               f"scene {meta['scene_id']}: {type(e).__name__}: {e}")
                image_meta = {"error": f"{type(e).__name__}: {str(e)[:200]}"}

        row["metadata"] = {
            "evidence": evidence,
            "image": image_meta,
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
                "candidate_cells": qa.field_pixels,
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
                 history: Optional[List[dict]] = None,
                 blocks: Optional[dict] = None,
                 ledger: Optional[Dict[str, list]] = None) -> Tuple[List[dict], dict]:
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

    geom_meas, buffer_applied, raw_area_m2, measured_area_m2 = measurement_field(geom)

    items = scenes if scenes is not None else search_s2(geom, days=lookback_days)
    report["items"] = len(items)
    items = dedupe_acquisitions(items, geom)
    report["deduped"] = report["items"] - len(items)

    # --- INCREMENTAL: skip scenes this pipeline already measured ------------
    # ledger: {scene_id: [(geometry_fingerprint, outcome), ...]} for THIS land.
    # A scene is skipped only if it was evaluated with the SAME geometry (a
    # boundary edit re-evaluates it) under the same pipeline version (checked
    # by the caller's query). Transient read failures are never in the ledger,
    # so they are retried.
    fingerprint = geometry_fingerprint(geom)
    report["geometry_fingerprint"] = fingerprint
    report["prior_accepted_in_window"] = False
    report["scenes_skipped_known"] = 0
    if ledger:
        pending = []
        for it in items:
            known = [o for fp, o in ledger.get(getattr(it, "id", None), []) if fp == fingerprint]
            if known:
                report["scenes_skipped_known"] += 1
                if "accepted" in known:
                    report["prior_accepted_in_window"] = True
            else:
                pending.append(it)
        items = pending
    report["pending_scene_ids"] = [getattr(it, "id", None) for it in items]

    rows = []
    report["scene_bands"] = {}
    for item in items:
        r = process_acquisition(item, geom_meas, buffer_applied, land, geom_conf,
                                block=(blocks or {}).get(getattr(item, "id", None)),
                                raw_area_m2=raw_area_m2,
                                reject_sink=report["optical_rejects"],
                                error_sink=report["scene_errors"],
                                measured_area_m2=measured_area_m2,
                                geom_wgs84=geom_meas,
                                band_sink=report["scene_bands"])
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

    # Nothing new was accepted, but an optical scene in this window WAS
    # accepted on an earlier night. The pre-ledger pipeline would have re-read
    # and re-accepted it, so it would NOT have fallen back to Sentinel-1.
    # Preserve that exactly: no radar fallback here.
    if report.get("prior_accepted_in_window"):
        logger.info(f"Land {land['id']}: no new optical acceptance; "
                    f"{report['scenes_skipped_known']} scene(s) already measured - unchanged")
        return [], report

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
    s1 = _process_s1(land, geom_meas, buffer_applied, geom_conf, raw_area_m2,
                     report, measured_area_m2)
    for row in s1:
        row.setdefault("metadata", {})["optical_rejects"] = rejects[:6]
    return s1, report


# ---------------------------------------------------------------------------
# SENTINEL-1 FALLBACK
# ---------------------------------------------------------------------------
def _process_s1(land: dict, geom_measured, buffer_applied: bool,
                geom_conf: str, raw_area_m2: float, report: dict,
                measured_area_m2: float = None) -> List[dict]:
    pairs = search_s1(geom_measured)
    if not pairs:
        return []

    collection, item = pairs[0]
    meta = acquisition_meta(item)
    _t0 = time.time()

    try:
        assets = {k.lower(): k for k in item.assets}
        # SAME coverage machinery as the optical path - one implementation.
        vv, ref_transform, ref_crs, coverage = read_band(item, assets["vv"], geom_measured)
        ref = (vv.shape, ref_transform, ref_crs, coverage)
        vh, _, _, _ = read_band(item, assets["vh"], geom_measured, reference=ref)

        n_fp = int(np.count_nonzero(coverage > 0))
        epc_total = float(coverage.sum())
        area_err = coverage_area_error(epc_total, measured_area_m2)

        res = rvi_from_gamma0(vv, vh, weights=coverage)
        epc_valid = float(res.get("epc") or 0.0)
        # Same purity definition as the optical path: EPC over the cells
        # that contributed, so radar is held to one standard, not two.
        s1_cells = int(res.get("valid_pixels") or 0)
        s1_purity = (epc_valid / s1_cells) if s1_cells else None
        s1_status, s1_ev = evidence_tier(epc_valid, s1_purity)
        if epc_valid < MIN_EPC:
            logger.info(f"REJECT radar | land={land['id']} | EPC {epc_valid:.2f} < {MIN_EPC}")
            return []
        _px = res.get("valid_pixels") or 0
        _s1_quality = 0.50 * min(epc_valid / EPC_SATURATION, 1.0)
        _area = land.get("area_acres")
        _s1_confidence = _s1_quality * 0.70
        if _area is not None and _area < MICRO_LAND_ACRES:
            _s1_confidence *= MICRO_LAND_FACTOR
        _s1_confidence *= GEOMETRY_CONFIDENCE_FACTOR.get(geom_conf, 0.5)
        if s1_ev in ("low", "insufficient"):
            _s1_confidence *= 0.75
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
            "effective_pixel_count": round(epc_valid, 4),
            "coverage_weighted_purity": round(s1_purity, 4) if s1_purity else None,
            "evidence_confidence": s1_ev,
            "measurement_status": s1_status,
            "spatial_stat_method": SPATIAL_STAT_METHOD,
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
                "evidence": {
                    "spatial_stat_method": SPATIAL_STAT_METHOD,
                    "effective_pixel_count": round(epc_valid, 4),
                    "effective_pixel_count_total": round(epc_total, 4),
                    "raw_valid_cell_count": n_fp,
                    "coverage_weighted_purity": round(s1_purity, 4) if s1_purity else None,
                    "measurement_status": s1_status,
                    "evidence_confidence": s1_ev,
                    "coverage_area_error": round(area_err, 4),
                    "measured_area_m2": round(measured_area_m2, 1) if measured_area_m2 else None,
                    "observed_or_predicted": "observed",
                },
                "pipeline_version": PIPELINE_VERSION,
            },
        }]
    except Exception as e:
        logger.warning(f"S1 processing failed for land {land['id']}: {e}")
        report["scene_errors"].append({"scene_id": meta.get("scene_id"),
                                       "error": f"S1 {type(e).__name__}: {str(e)[:300]}"})
        return []
