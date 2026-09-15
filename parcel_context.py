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
from indices import compute_indices, weighted_index_statistics

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


def extract_parcel_context(
    item,
    parcel_geom_wgs84,
    context_geom_wgs84=None,
    target_area_m2: float = CONTEXT_TARGET_AREA_M2,
) -> Optional[dict]:
    """Extract observed local context from exactly one Sentinel-2 scene."""
    if not np.isfinite(float(target_area_m2)) or float(target_area_m2) <= 0:
        raise ValueError("target_area_m2 must be finite and positive")

    context_meta = adaptive_context_geometry(
        parcel_geom_wgs84, target_total_area_m2=float(target_area_m2)
    ) if context_geom_wgs84 is None else None
    if context_geom_wgs84 is None:
        context_geom_wgs84 = context_meta.geometry_wgs84

    b04, transform, crs, context_cov = read_band(item, "B04", context_geom_wgs84)
    ref = (b04.shape, transform, crs, context_cov)
    b08, _, _, _ = read_band(item, "B08", context_geom_wgs84, reference=ref)
    scl, _, _, _ = read_band(item, "SCL", context_geom_wgs84, reference=ref, categorical=True)

    masks = scl_masks(scl, coverage=context_cov)
    ndvi = compute_indices({"B04": b04, "B08": b08}).get("NDVI")
    if ndvi is None:
        return None

    parcel_cov, exclusion_method = _target_fraction_on_context_grid(
        parcel_geom_wgs84, transform, crs, b04.shape
    )
    context_only = np.clip(
        context_cov - np.minimum(context_cov, parcel_cov), 0.0, 1.0
    )
    valid = np.isfinite(ndvi) & masks["crop"]
    weights = np.where(valid, context_only, 0.0).astype("float32")
    stats = weighted_index_statistics(ndvi, weights)
    if not stats or stats.get("epc", 0.0) <= 0:
        return None

    robust = summarize_context(ndvi, weights, parcel_ndvi=None)
    valid_crop_area_m2 = float(stats["epc"] * 100.0)
    observed_context_fraction = valid_crop_area_m2 / max(float(target_area_m2), 1.0)

    out = {
        "status": "observed_context",
        "source": "sentinel-2",
        "scene_id": item.id,
        "acquisition_time": item.datetime.isoformat() if item.datetime else None,
        "native_resolution_m": 10,
        "target_context_area_m2": float(target_area_m2),
        "context_geometry_area_m2": round(float(context_meta.achieved_total_area_m2), 2) if context_meta else None,
        "context_buffer_m": round(float(context_meta.buffer_m), 3) if context_meta else None,
        "context_valid_crop_area_m2": round(valid_crop_area_m2, 2),
        "context_observed_fraction_of_target": round(observed_context_fraction, 4),
        "context_effective_pixel_count": round(float(stats["epc"]), 4),
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
        "context_purity": stats.get("purity"),
        "context_interior_share": stats.get("interior_share"),
        "context_boundary_share": stats.get("boundary_share"),
        "context_crop_fraction": masks.get("crop_fraction"),
        "context_cloud_fraction": masks.get("cloud_fraction"),
        "context_shadow_fraction": masks.get("shadow_fraction"),
        "context_water_fraction": masks.get("water_fraction"),
        "context_snow_fraction": masks.get("snow_fraction"),
        "context_saturated_fraction": masks.get("saturated_fraction"),
        "parcel_exclusion_method": exclusion_method,
        "context_weight_method": "exact_fractional_context_minus_parcel_v2",
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
    return out


def add_parcel_delta(context: dict, parcel_ndvi: Optional[float]) -> dict:
    """Attach parcel-vs-context anomaly without changing either value."""
    out = dict(context)
    med = context.get("context_ndvi_median")
    mad = context.get("context_ndvi_mad")
    if parcel_ndvi is None or med is None:
        out["parcel_context_delta"] = None
        out["parcel_context_robust_z"] = None
        return out
    delta = float(parcel_ndvi) - float(med)
    out["parcel_context_delta"] = delta
    out["parcel_context_robust_z"] = (
        delta / (1.4826 * float(mad)) if mad is not None and float(mad) > 1e-9 else None
    )
    return out
