"""Same-scene Sentinel-2 local context for smallholder parcel intelligence.

The farmer polygon remains the measurement target. The expanded geometry is
only a local reference population. Context values never overwrite ndvi_value.
"""
from __future__ import annotations

from typing import Optional
import numpy as np
from pyproj import Transformer
from rasterio.features import geometry_mask
from shapely.geometry import mapping
from shapely.ops import transform as shp_transform

from parcel_intelligence import adaptive_context_geometry, summarize_context
from raster_utils import coverage_fractions, read_band, scl_masks
from config import (MIN_EPC, INTERIOR_COVERAGE, CONTEXT_RING_GAP_M, CONTEXT_RING_MAX_M,
                    CONTEXT_CLEAN_PX_REAL, CONTEXT_CLEAN_PX_MIN)
from indices import compute_indices, weighted_index_statistics

# Output version - part of the scene ledger identity (main.PROCESSING_IDENTITY).
# Bump it whenever the neighbour ring + combined-uncertainty z changes, so every scene is re-evaluated.
CONTEXT_VERSION = "context-ring-v3"

CONTEXT_TARGET_AREA_M2 = 4046.8564224  # 40 guntha; configurable.
CONTEXT_BANDS_10M = ("B04", "B08")


def _project(geom, src_crs):
    return shp_transform(
        Transformer.from_crs("EPSG:4326", src_crs, always_xy=True).transform,
        geom,
    )


def _target_fraction_on_context_grid(parcel_geom, transform, crs, shape):
    parcel_proj = _project(parcel_geom, crs)
    cov, method = coverage_fractions(parcel_proj, transform, shape)
    if cov is None:
        mask = geometry_mask(
            [mapping(parcel_proj)], out_shape=shape, transform=transform,
            invert=True, all_touched=True,
        )
        cov = mask.astype("float32")
        method = "binary_target_fallback"
    return cov, method


def _ring_window_wgs84(parcel_geom_wgs84, ring_max_m: float):
    """Read window: the parcel grown by ring_max_m, in a local metric CRS."""
    c = parcel_geom_wgs84.centroid
    zone = int((c.x + 180.0) // 6.0) + 1
    utm = f"EPSG:{32600 + zone if c.y >= 0 else 32700 + zone}"
    to_m = Transformer.from_crs("EPSG:4326", utm, always_xy=True).transform
    to_d = Transformer.from_crs(utm, "EPSG:4326", always_xy=True).transform
    return shp_transform(to_d, shp_transform(to_m, parcel_geom_wgs84).buffer(float(ring_max_m)))


def extract_parcel_context(
    item,
    parcel_geom_wgs84,
    context_geom_wgs84=None,
    target_area_m2: float = CONTEXT_TARGET_AREA_M2,
    block=None,
) -> Optional[dict]:
    """Observed neighbour context for one field from one Sentinel-2 scene.

    NEIGHBOUR RING (agronomic decision 2026-09-23, applies to every land):
      * neighbours are WHOLE clean pixels outside the field - no cell that the
        field's polygon touches, and nothing within CONTEXT_RING_GAP_M of the
        boundary (the bund / shared edge pixel belongs to neither side);
      * the ring grows outward in 10 m steps until it holds
        CONTEXT_CLEAN_PX_REAL clean crop pixels, capped at CONTEXT_RING_MAX_M
        so it stays a neighbourhood. Every field size gets a ring - the old
        "grow until TOTAL area = 40 guntha" rule gave fields over 1 acre no ring
        and fields just under 1 acre a ring a few centimetres wide;
      * tier: >= CONTEXT_CLEAN_PX_REAL pixels = real; CONTEXT_CLEAN_PX_MIN..20
        = indicative (difference published, no verdict); below = no comparison.
    target_area_m2 is kept for signature compatibility and provenance only.
    """
    from shapely import points as _points, distance as _distance
    if context_geom_wgs84 is None:
        context_geom_wgs84 = _ring_window_wgs84(parcel_geom_wgs84, CONTEXT_RING_MAX_M)

    # Served from the shared tile-scene block when one was read (padded by
    # tile_reader.CONTEXT_BLOCK_PAD_M so the ring fits); tile_reader.subset_band
    # reproduces read_band exactly and refuses any window outside the block.
    def _band(bk, reference=None, categorical=False):
        if block is not None:
            try:
                from tile_reader import subset_band
                got = subset_band(block, bk, context_geom_wgs84, reference=reference,
                                  categorical=categorical)
                if got is not None:
                    return got
            except Exception:
                pass
        return read_band(item, bk, context_geom_wgs84, reference=reference,
                         categorical=categorical)

    b04, transform, crs, context_cov = _band("B04")
    ref = (b04.shape, transform, crs, context_cov)
    b08, _, _, _ = _band("B08", reference=ref)
    scl, _, _, _ = _band("SCL", reference=ref, categorical=True)

    masks = scl_masks(scl, coverage=context_cov)
    ndvi = compute_indices({"B04": b04, "B08": b08}).get("NDVI")
    if ndvi is None:
        return None

    parcel_cov, exclusion_method = _target_fraction_on_context_grid(
        parcel_geom_wgs84, transform, crs, b04.shape
    )
    # candidate neighbour pixels: whole cell inside the read window, untouched
    # by the field polygon, clear crop surface, finite NDVI
    cand = ((context_cov >= INTERIOR_COVERAGE) & (parcel_cov <= 0.0)
            & masks["crop"] & np.isfinite(ndvi))
    rr, cc = np.nonzero(cand)
    parcel_proj = _project(parcel_geom_wgs84, crs)
    if rr.size:
        xs, ys = transform * (cc + 0.5, rr + 0.5)
        dist = np.asarray(_distance(_points(np.asarray(xs), np.asarray(ys)), parcel_proj), dtype="float64")
    else:
        dist = np.zeros(0)
    beyond_gap = dist >= CONTEXT_RING_GAP_M

    ring_m = CONTEXT_RING_MAX_M
    w = CONTEXT_RING_GAP_M + 10.0
    while w < CONTEXT_RING_MAX_M:
        if int(np.count_nonzero(beyond_gap & (dist <= w))) >= CONTEXT_CLEAN_PX_REAL:
            ring_m = w
            break
        w += 10.0
    chosen = beyond_gap & (dist <= ring_m)
    n_clean = int(np.count_nonzero(chosen))

    base = {"source": "sentinel-2", "scene_id": item.id,
            "context_clean_pixels": n_clean, "context_buffer_m": float(ring_m),
            "context_ring_gap_m": float(CONTEXT_RING_GAP_M),
            "context_ring_max_m": float(CONTEXT_RING_MAX_M)}
    if n_clean < CONTEXT_CLEAN_PX_MIN:
        return {**base, "status": "insufficient_context",
                "reason": "fewer_clean_neighbour_pixels_than_min",
                "context_effective_pixel_count": float(n_clean),
                "min_clean_pixels": int(CONTEXT_CLEAN_PX_MIN)}

    weights = np.zeros(b04.shape, dtype="float32")
    weights[rr[chosen], cc[chosen]] = 1.0          # whole clean pixels, equal weight
    stats = weighted_index_statistics(ndvi, weights)
    if not stats:
        return None
    robust = summarize_context(ndvi, weights, parcel_ndvi=None)
    tier = "real" if n_clean >= CONTEXT_CLEAN_PX_REAL else "indicative"

    return {
        **base,
        "status": "observed_context",
        "context_tier": tier,
        "acquisition_time": item.datetime.isoformat() if item.datetime else None,
        "native_resolution_m": 10,
        "target_context_area_m2": float(target_area_m2),
        "context_valid_crop_area_m2": round(float(n_clean) * 100.0, 2),
        "context_effective_pixel_count": float(n_clean),
        "context_raw_valid_cells": int(stats["n_cells"]),
        "context_ndvi_mean": float(stats["mean"]),
        "context_ndvi_median": float(stats["median"]),
        "context_ndvi_p10": float(stats["p10"]),
        "context_ndvi_p90": float(stats["p90"]),
        "context_ndvi_std": float(stats["std"]),
        "context_ndvi_mad": robust.mad_ndvi,
        "context_ndvi_min": float(stats["min"]),
        "context_ndvi_max": float(stats["max"]),
        "context_ndvi_se": stats.get("se"),
        "context_purity": 1.0,
        "context_crop_fraction": masks.get("crop_fraction"),
        "context_cloud_fraction": masks.get("cloud_fraction"),
        "context_shadow_fraction": masks.get("shadow_fraction"),
        "context_water_fraction": masks.get("water_fraction"),
        "context_snow_fraction": masks.get("snow_fraction"),
        "context_saturated_fraction": masks.get("saturated_fraction"),
        "parcel_exclusion_method": exclusion_method,
        "context_weight_method": "clean_neighbour_ring_v3",
        "masking": {
            "scl_crop_surface": "production_scl_masks",
            "cloud_shadow_dilation_px": masks.get("cloud_dilation_px"),
        },
        "provenance": {
            "bands": list(CONTEXT_BANDS_10M),
            "grid": "B04_native_10m",
            "observed_or_estimated": "observed",
            "context_is_not_parcel_measurement": True,
        },
    }


def add_parcel_delta(context: dict, parcel_ndvi: Optional[float],
                     parcel_se: Optional[float] = None) -> dict:
    """Parcel-vs-neighbours difference and robust z, neither value changed.

    z = delta / sqrt((1.4826*MAD)^2 + SE_field^2 + SE_ring^2)
    The old denominator used the neighbour spread alone, so a small, uniform
    ring (MAD near zero) inflated z into a false "behind"/"ahead". Measurement
    uncertainty of both sides now enters it. SE_ring is the standard error of
    a median, 1.2533 * sigma / sqrt(n).
    A z (the app's verdict) is published only for a REAL ring; an indicative
    ring publishes the difference with its tier, never a verdict.
    """
    out = dict(context)
    med = context.get("context_ndvi_median")
    mad = context.get("context_ndvi_mad")
    if parcel_ndvi is None or med is None:
        out["parcel_context_delta"] = None
        out["parcel_context_robust_z"] = None
        return out
    delta = float(parcel_ndvi) - float(med)
    out["parcel_context_delta"] = delta
    out["parcel_context_robust_z"] = None
    if context.get("context_tier", "real") != "real":
        return out
    n = float(context.get("context_clean_pixels") or context.get("context_effective_pixel_count") or 0.0)
    sigma = 1.4826 * float(mad) if mad is not None else None
    if sigma is None:
        return out
    se_ring = 1.2533 * sigma / np.sqrt(n) if n > 0 else 0.0   # older rows carry no pixel count
    denom = float(np.sqrt(sigma ** 2 + float(parcel_se or 0.0) ** 2 + se_ring ** 2))
    out["parcel_context_robust_z"] = (delta / denom) if denom > 1e-9 else None
    out["parcel_context_z_denominator"] = round(denom, 6)
    return out
