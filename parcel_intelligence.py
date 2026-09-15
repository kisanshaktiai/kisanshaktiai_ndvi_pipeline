"""
Parcel-constrained smallholder intelligence primitives.

This module deliberately separates:
  OBSERVED: native satellite measurements;
  ESTIMATED: modelled/reconstructed quantities;
  INTERPRETED: agronomic hypotheses.

No function in this module claims sub-pixel reconstruction is ground truth.
The current implementation provides geometry-aware context construction,
robust local-reference statistics, and a conservative two-endmember spectral
unmixing primitive that is suitable for calibration experiments.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

import numpy as np
from pyproj import CRS, Transformer
from scipy.optimize import nnls
from shapely.geometry.base import BaseGeometry
from shapely.ops import transform as shp_transform


@dataclass(frozen=True)
class ContextGeometry:
    geometry_wgs84: BaseGeometry
    buffer_m: float
    target_total_area_m2: float
    achieved_total_area_m2: float
    context_area_m2: float


@dataclass(frozen=True)
class ContextSummary:
    n_cells: int
    effective_cells: float
    median_ndvi: Optional[float]
    mad_ndvi: Optional[float]
    p10_ndvi: Optional[float]
    p90_ndvi: Optional[float]
    parcel_minus_context: Optional[float]
    robust_z: Optional[float]


def _utm_for(geom: BaseGeometry) -> CRS:
    """Return a local metric CRS for buffering/area calculations."""
    lon = float(geom.centroid.x)
    lat = float(geom.centroid.y)
    zone = int((lon + 180.0) // 6.0) + 1
    return CRS.from_epsg((32600 if lat >= 0 else 32700) + zone)


def adaptive_context_geometry(
    geom_wgs84: BaseGeometry,
    target_total_area_m2: float = 4046.8564224,
    max_buffer_m: float = 250.0,
    iterations: int = 28,
) -> ContextGeometry:
    """
    Expand the exact parcel until the TOTAL context footprint reaches the
    requested area, using metric buffering and binary search.

    40 guntha is approximately 4046.86 m2, but area is only a target for the
    context footprint. The farmer's polygon is never replaced by this shape.
    """
    if geom_wgs84.is_empty or target_total_area_m2 <= 0:
        return ContextGeometry(geom_wgs84, 0.0, target_total_area_m2, 0.0, 0.0)

    utm = _utm_for(geom_wgs84)
    fwd = Transformer.from_crs("EPSG:4326", utm, always_xy=True).transform
    inv = Transformer.from_crs(utm, "EPSG:4326", always_xy=True).transform
    base = shp_transform(fwd, geom_wgs84)
    base_area = float(base.area)

    if base_area >= target_total_area_m2:
        return ContextGeometry(geom_wgs84, 0.0, target_total_area_m2, base_area, 0.0)

    lo, hi = 0.0, float(max_buffer_m)
    while base.buffer(hi).area < target_total_area_m2 and hi < 2000.0:
        hi *= 2.0
        if hi == 0:
            hi = 1.0

    hi = min(hi, 2000.0)
    for _ in range(max(1, iterations)):
        mid = (lo + hi) / 2.0
        if base.buffer(mid).area >= target_total_area_m2:
            hi = mid
        else:
            lo = mid

    buffered = base.buffer(hi)
    out = shp_transform(inv, buffered)
    achieved = float(buffered.area)
    return ContextGeometry(
        geometry_wgs84=out,
        buffer_m=float(hi),
        target_total_area_m2=float(target_total_area_m2),
        achieved_total_area_m2=achieved,
        context_area_m2=max(0.0, achieved - base_area),
    )


def robust_location(values: np.ndarray, weights: Optional[np.ndarray] = None) -> tuple[Optional[float], Optional[float]]:
    """Weighted median and weighted MAD; ignores non-finite values."""
    v = np.asarray(values, dtype="float64").ravel()
    ok = np.isfinite(v)
    if weights is None:
        w = np.ones_like(v)
    else:
        w = np.asarray(weights, dtype="float64").ravel()
    ok &= np.isfinite(w) & (w > 0)
    v, w = v[ok], w[ok]
    if v.size == 0 or w.sum() <= 0:
        return None, None
    order = np.argsort(v)
    v, w = v[order], w[order]
    c = np.cumsum(w) / w.sum()
    med = float(v[np.searchsorted(c, 0.5, side="left")])
    dev = np.abs(v - med)
    order2 = np.argsort(dev)
    dev, w2 = dev[order2], w[order2]
    c2 = np.cumsum(w2) / w2.sum()
    mad = float(dev[np.searchsorted(c2, 0.5, side="left")])
    return med, mad


def summarize_context(
    context_ndvi: np.ndarray,
    context_weights: np.ndarray,
    parcel_ndvi: Optional[float],
) -> ContextSummary:
    """Summarise a local reference population without changing parcel NDVI."""
    v = np.asarray(context_ndvi, dtype="float64")
    w = np.asarray(context_weights, dtype="float64")
    ok = np.isfinite(v) & np.isfinite(w) & (w > 0)
    vv, ww = v[ok], w[ok]
    if vv.size == 0:
        return ContextSummary(0, 0.0, None, None, None, None, None, None)

    med, mad = robust_location(vv, ww)
    order = np.argsort(vv)
    sv, sw = vv[order], ww[order]
    cw = np.cumsum(sw) / sw.sum()

    def q(p: float) -> float:
        return float(sv[np.searchsorted(cw, p, side="left")])

    delta = (float(parcel_ndvi) - med) if parcel_ndvi is not None and med is not None else None
    robust_z = None
    if delta is not None and mad is not None:
        robust_z = float(delta / (1.4826 * mad)) if mad > 1e-9 else (0.0 if abs(delta) < 1e-9 else float(np.sign(delta) * np.inf))

    return ContextSummary(
        n_cells=int(vv.size),
        effective_cells=float(ww.sum()),
        median_ndvi=med,
        mad_ndvi=mad,
        p10_ndvi=q(0.10),
        p90_ndvi=q(0.90),
        parcel_minus_context=delta,
        robust_z=robust_z,
    )


def constrained_unmix(
    observed_spectrum: Sequence[float],
    endmembers: Sequence[Sequence[float]],
    nonnegative: bool = True,
) -> dict:
    """
    Solve a conservative non-negative linear spectral mixture.

    Returns estimated fractions, reconstructed spectrum and residual RMSE.
    Fractions are normalised to sum to one only when the unconstrained total is
    positive. This is an estimation primitive; it must be calibrated before
    being promoted to a production crop-only NDVI estimator.
    """
    y = np.asarray(observed_spectrum, dtype="float64").ravel()
    E = np.asarray(endmembers, dtype="float64")
    if E.ndim != 2 or y.ndim != 1 or E.shape[1] != y.size:
        raise ValueError("endmembers must have shape (n_endmembers, n_bands) matching observed_spectrum")
    if not np.isfinite(y).all() or not np.isfinite(E).all():
        raise ValueError("spectral inputs must be finite")

    A = E.T
    if nonnegative:
        fractions, _ = nnls(A, y)
    else:
        fractions, *_ = np.linalg.lstsq(A, y, rcond=None)
        fractions = np.asarray(fractions, dtype="float64")

    fractions = np.clip(fractions, 0.0, None)
    total = float(fractions.sum())
    if total > 0:
        fractions /= total
    reconstructed = fractions @ E
    residual = y - reconstructed
    return {
        "fractions": fractions.tolist(),
        "reconstructed_spectrum": reconstructed.tolist(),
        "residual_rmse": float(np.sqrt(np.mean(residual * residual))),
        "sum_to_one": float(fractions.sum()),
        "method": "nonnegative_linear_unmixing_nnls",
        "status": "estimated_unvalidated",
    }
