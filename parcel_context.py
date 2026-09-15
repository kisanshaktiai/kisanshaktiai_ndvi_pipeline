"""
Parcel context extraction from the SAME Sentinel-2 acquisition.

The farmer polygon remains the measurement target. This module creates an
expanded context footprint only to obtain surrounding pixels, then subtracts
the target parcel with exact fractional geometry on the native 10 m grid.
Context statistics are evidence for reconstruction; they are never substituted
for the observed parcel NDVI.
"""

from __future__ import annotations

from typing import Optional
import numpy as np
from shapely.geometry import mapping
from shapely.ops import transform as shp_transform
from pyproj import Transformer
from rasterio.features import geometry_mask

from parcel_intelligence import adaptive_context_geometry, summarize_context
from raster_utils import coverage_fractions, read_band, scl_masks
from indices import compute_indices, weighted_index_statistics


CONTEXT_TARGET_AREA_M2 = 4046.8564224  # 40 guntha; configurable at call site.
CONTEXT_BANDS_10M = ("B04", "B08")


def _project(geom, src_crs):
    t = Transformer.from_crs("EPSG:4326", src_crs, always_xy=True).transform
    return shp_transform(t, geom)


def _target_fraction_on_context_grid(parcel_geom, transform, crs, shape):
    parcel_proj = _project(parcel_geom, crs)
    cov, method = coverage_fractions(parcel_proj, transform, shape)
    if cov is None:
        # Context windows are deliberately bounded; this is a safety fallback.
        mask = geometry_mask([mapping(parcel_proj)], out_shape=shape,
                             transform=transform, invert=True, all_touched=True)
        cov = mask.astype("float32")
        method = "binary_target_fallback"
    return cov, method


def extract_parcel_context(
    item,
    parcel_geom_wgs84,
    context_geom_wgs84=None,
    target_area_m2: float = CONTEXT_TARGET_AREA_M2,
) -> Optional[dict]:
    """
    Extract context pixels from one Sentinel-2 acquisition.

    Context = expanded footprint minus the exact farmer parcel. All returned
    statistics are based on the same native 10 m B04/B08 grid used by the
    parcel observation. Cloud/non-crop masking follows the production SCL
    rules. No temporal interpolation is performed.
    """
    if context_geom_wgs84 is None:
        context_geom_wgs84 = adaptive_context_geometry(
            parcel_geom_wgs84, target_total_area_m2=target_area_m2
        ).geometry_wgs84

    # B04 defines the native 10 m context grid.
    b04, transform, crs, context_cov = read_band(
        item, "B04", context_geom_wgs84
    )
    ref = (b04.shape, transform, crs, context_cov)
    b08, _, _, _ = read_band(item, "B08", context_geom_wgs84, reference=ref)
    scl, _, _, _ = read_band(
        item, "SCL", context_geom_wgs84, reference=ref, categorical=True
    )

    masks = scl_masks(scl, coverage=context_cov)
    indices = compute_indices({"B04": b04, "B08": b08})
    ndvi = indices.get("NDVI")
    if ndvi is None:
        return None

    parcel_cov, parcel_cov_method = _target_fraction_on_context_grid(
        parcel_geom_wgs84, transform, crs, b04.shape
    )

    # Context-only weight: native context footprint × valid crop surface ×
    # complement of exact parcel coverage. Partial boundary cells therefore
    # contribute only the surrounding fraction rather than being duplicated.
    context_only = np.clip(context_cov - np.minimum(context_cov, parcel_cov), 0.0, 1.0)
    valid = np.isfinite(ndvi) & masks["crop"]
    weights = np.where(valid, context_only, 0.0).astype("float32")

    stats = weighted_index_statistics(ndvi, weights)
    if not stats or stats.get("epc", 0.0) <= 0:
        return None

    parcel_stats = summarize_context(
        ndvi,
        weights,
        parcel_ndvi=None,
    )

    # Context area is the weighted number of 10 m cells × 100 m².
    context_area_m2 = float(weights.sum() * 100.0)
    context_fraction_of_target = context_area_m2 / max(float(target_area_m2), 1.0)

    return {
        "status": "observed_context",
        "source": "sentinel-2",
        "scene_id": item.id,
        "acquisition_time": item.datetime.isoformat() if item.datetime else None,
        "native_resolution_m": 10,
        "target_context_area_m2": float(target_area_m2),
        "context_area_m2": round(context_area_m2, 2),
        "context_fraction_of_target": round(context_fraction_of_target, 4),
        "context_effective_pixel_count": round(float(stats["epc"]), 4),
        "context_raw_valid_cells": int(stats["n_cells"]),
        "context_ndvi_mean": float(stats["mean"]),
        "context_ndvi_median": float(stats["median"]),
        "context_ndvi_p10": float(stats["p10"]),
        "context_ndvi_p90": float(stats["p90"]),
        "context_ndvi_std": float(stats["std"]),
        "context_ndvi_mad": parcel_stats.mad_ndvi,
        "context_ndvi_min": float(stats["min"]),
        "context_ndvi_max": float(stats["max"]),
        "context_ndvi_se": stats.get("se"),
        "context_purity": stats.get("purity"),
        "context_interior_share": stats.get("interior_share"),
        "context_boundary_share": stats.get("boundary_share"),
        "parcel_exclusion_method": parcel_cov_method,
        "context_weight_method": "exact_fractional_context_minus_parcel_v1",
        "masking": {
            "scl_crop_surface": "production_scl_masks",
            "cloud_shadow_dilation": "production_scl_masks",
            "cloud_fraction": masks.get("cloud_fraction"),
            "shadow_fraction": masks.get("shadow_fraction"),
        },
        "provenance": {
            "bands": list(CONTEXT_BANDS_10M),
            "grid": "B04_native_10m",
            "observed_or_estimated": "observed",
            "context_is_not_parcel_measurement": True,
        },
    }


def add_parcel_delta(context: dict, parcel_ndvi: Optional[float]) -> dict:
    """Attach parcel-vs-context anomaly without modifying either observation."""
    out = dict(context)
    if parcel_ndvi is None:
        out["parcel_context_delta"] = None
        out["parcel_context_robust_z"] = None
        return out
    med = context.get("context_ndvi_median")
    mad = context.get("context_ndvi_mad")
    delta = float(parcel_ndvi) - float(med) if med is not None else None
    robust_z = None
    if delta is not None and mad is not None:
        robust_z = delta / (1.4826 * float(mad)) if float(mad) > 1e-9 else None
    out["parcel_context_delta"] = delta
    out["parcel_context_robust_z"] = robust_z
    return out
