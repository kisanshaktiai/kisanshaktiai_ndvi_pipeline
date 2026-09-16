"""Production intelligence primitives for smallholder satellite evidence.

This module is deliberately agronomy-neutral: it computes measurement/evidence
features only. Agronomic thresholds and decisions remain in the Decision Brain.

Invariants:
- observed NDVI is never overwritten by an estimate;
- B8A/B05 is the canonical Sentinel-2 NDRE pair;
- spatial support accounts for correlation rather than treating every 10 m cell
  as independent;
- model-derived values remain non-farmer-facing until independently validated.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import math
from typing import Iterable, Mapping, Optional, Sequence

import numpy as np


INTELLIGENCE_STATUSES = {
    "observed_context",
    "estimated_unvalidated",
    "validated",
}


def calibration_provenance(item, band_key: str) -> dict:
    """Return auditable calibration metadata without changing the measurement."""
    scale = 1.0 / 10000.0
    offset = 0.0
    source = "sentinel2_default"
    baseline = None
    metadata_found = False
    try:
        baseline_raw = item.properties.get("s2:processing_baseline")
        baseline = float(baseline_raw) if baseline_raw not in (None, "") else None
    except (TypeError, ValueError):
        baseline = None
    try:
        rb = (item.assets[band_key].extra_fields.get("raster:bands") or [{}])[0] or {}
        if rb.get("scale") is not None:
            scale = float(rb["scale"])
            metadata_found = True
        if rb.get("offset") is not None:
            offset = float(rb["offset"])
            metadata_found = True
        if metadata_found:
            source = "stac_raster_bands"
    except (AttributeError, KeyError, TypeError, ValueError):
        pass
    if not metadata_found and baseline is not None and baseline >= 4.0:
        offset = -1000.0 * scale
        source = "esa_boa_add_offset_fallback"
    if not np.isfinite(scale) or scale <= 0:
        raise ValueError(f"Invalid calibration scale for {band_key}: {scale!r}")
    if not np.isfinite(offset):
        raise ValueError(f"Invalid calibration offset for {band_key}: {offset!r}")
    return {
        "band": band_key,
        "scale": scale,
        "offset": offset,
        "source": source,
        "processing_baseline": baseline,
        "formula": "physical = raw * scale + offset",
    }


def ndre_b8a_b05(b8a: np.ndarray, b05: np.ndarray) -> np.ndarray:
    """Canonical Sentinel-2 NDRE using B8A (865 nm) and B05 (705 nm)."""
    a = np.asarray(b8a, dtype="float32")
    b = np.asarray(b05, dtype="float32")
    denom = a + b
    with np.errstate(divide="ignore", invalid="ignore"):
        out = (a - b) / np.where(denom > 1e-6, denom, np.nan)
    return out.astype("float32", copy=False)


def _pairwise_variogram(values: np.ndarray, rows: np.ndarray, cols: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Empirical semivariogram samples using all unique cell pairs."""
    n = len(values)
    if n < 3:
        return np.array([]), np.array([])
    d = []
    g = []
    for i in range(n - 1):
        dv = values[i + 1:] - values[i]
        dist = np.hypot(rows[i + 1:] - rows[i], cols[i + 1:] - cols[i])
        d.extend(dist.tolist())
        g.extend((0.5 * dv * dv).tolist())
    return np.asarray(d, dtype="float64"), np.asarray(g, dtype="float64")


def spatial_effective_sample_size(values: np.ndarray, weights: np.ndarray) -> dict:
    """Estimate correlation-adjusted support from the empirical semivariogram.

    Distances are in reference-grid cells. No universal agronomic correlation
    length is assumed. If a stable sill/range cannot be estimated, the result
    explicitly reports insufficient spatial evidence and falls back to Kish
    support only as a conservative lower-information state.
    """
    v = np.asarray(values, dtype="float64")
    w = np.asarray(weights, dtype="float64")
    ok = np.isfinite(v) & (w > 0)
    if not ok.any():
        return {"n_eff_spatial": 0.0, "spatial_se": None, "method": "insufficient"}
    flat = np.flatnonzero(ok)
    rows, cols = np.unravel_index(flat, v.shape)
    vv, ww = v[ok], w[ok]
    kish = float(ww.sum() ** 2 / np.sum(ww ** 2))
    if len(vv) < 4 or float(np.nanstd(vv)) <= 1e-9:
        return {
            "n_eff_spatial": round(kish, 4),
            "spatial_se": 0.0 if len(vv) >= 2 else None,
            "method": "insufficient_variogram_support_kish",
        }

    dist, gamma = _pairwise_variogram(vv, rows.astype(float), cols.astype(float))
    if dist.size < 6:
        return {"n_eff_spatial": round(kish, 4), "spatial_se": None, "method": "insufficient_variogram_pairs"}

    sill = float(np.nanpercentile(gamma, 90))
    nugget = float(np.nanpercentile(gamma[dist <= np.nanpercentile(dist, 25)], 10)) if np.any(dist > 0) else 0.0
    target = nugget + 0.95 * max(sill - nugget, 0.0)
    order = np.argsort(dist)
    sd = dist[order]
    sg = gamma[order]
    reached = sd[sg >= target]
    if reached.size == 0 or not np.isfinite(target):
        return {"n_eff_spatial": round(kish, 4), "spatial_se": None, "method": "unresolved_range_kish"}
    range_cells = max(float(np.min(reached)), 1.0)
    # Effective independent support is approximated by the number of
    # correlation areas represented by the weighted footprint. It can never
    # exceed Kish support or the physical EPC.
    epc = float(ww.sum())
    corr_area = math.pi * range_cells * range_cells
    n_spatial = max(1.0, min(kish, epc / corr_area))
    mean = float(np.sum(vv * ww) / np.sum(ww))
    var = float(np.sum(ww * (vv - mean) ** 2) / np.sum(ww))
    se = math.sqrt(max(var, 0.0) / n_spatial)
    return {
        "n_eff_spatial": round(n_spatial, 4),
        "spatial_se": round(se, 6),
        "variogram_range_cells": round(range_cells, 4),
        "variogram_sill": round(sill, 6),
        "variogram_nugget": round(nugget, 6),
        "method": "empirical_variogram",
    }


def uncertainty_vector(*, measurement_quality: Optional[float], spatial_support: Optional[float],
                       temporal_support: Optional[float], agreement: Optional[float],
                       model_confidence: Optional[float]) -> dict:
    """Create the canonical vector and conservative minimum gate."""
    vals = [x for x in (measurement_quality, spatial_support, temporal_support, agreement, model_confidence)
            if x is not None]
    gate = min(vals) if vals else None
    return {
        "measurement_quality": measurement_quality,
        "spatial_support": spatial_support,
        "temporal_support": temporal_support,
        "agreement": agreement,
        "model_confidence": model_confidence,
        "minimum_gate": gate,
    }


def cohort_key(*, crop_code: str, sowing_or_transplant_date: Optional[date], block_id: Optional[str]) -> Optional[str]:
    """Primary TATVA reference-population key; unknown date means unavailable."""
    if not crop_code or sowing_or_transplant_date is None or not block_id:
        return None
    # ISO week is a stable, language-independent cohort key.
    iso = sowing_or_transplant_date.isocalendar()
    return f"{str(crop_code).strip().lower()}:{iso.year}-W{iso.week:02d}:{block_id}"


def choose_context_population(*, cohort_size: int, configured_min_cohort: int, ring_available: bool) -> str:
    """Cohort is primary; spatial ring is a secondary fallback only."""
    if cohort_size >= configured_min_cohort:
        return "stage_cohort"
    if ring_available:
        return "spatial_ring_fallback"
    return "none"


def water_stress_evidence(*, ndmi_decline: Optional[bool], root_zone_depletion: Optional[bool],
                          rain_deficit: Optional[bool], water_sensitive_stage: Optional[bool],
                          landsat_lst_support: Optional[bool] = None) -> dict:
    """Evidence gate only; does not assign agronomic thresholds."""
    core = [ndmi_decline, root_zone_depletion, rain_deficit, water_sensitive_stage]
    corroborators = sum(x is True for x in core)
    return {
        "ndmi_decline": ndmi_decline,
        "root_zone_depletion": root_zone_depletion,
        "rain_deficit": rain_deficit,
        "water_sensitive_stage": water_sensitive_stage,
        "landsat_lst_support": landsat_lst_support,
        "core_evidence_count": corroborators,
        "status": "corroborated" if corroborators == 4 else "insufficient_evidence",
    }


def temporal_support(*, days_since_last_clear: Optional[float], gap_limit_days: Optional[float]) -> Optional[float]:
    """Monotonic support score; the limit is configuration, never hardcoded."""
    if days_since_last_clear is None or gap_limit_days is None or gap_limit_days <= 0:
        return None
    return max(0.0, min(1.0, 1.0 - float(days_since_last_clear) / float(gap_limit_days)))


def spatial_zone_gate(*, interior_valid_cells: int, minimum_cells: int,
                      quadrant_kappa: Optional[float], minimum_kappa: float,
                      confirmed_cases: int, minimum_cases: int) -> bool:
    """Whether a within-field zone may become decision-visible."""
    return (interior_valid_cells >= minimum_cells and quadrant_kappa is not None
            and quadrant_kappa >= minimum_kappa and confirmed_cases >= minimum_cases)
