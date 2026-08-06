"""
sar_vegetation.py - Sentinel-1 Radar Vegetation Index (monsoon fallback).

WHY THIS EXISTS
---------------
Optical NDVI is unavailable under monsoon cloud. Evidence from production:
the pipeline skipped 100% of lands in August 2026 and 99.1% in July.
Kharif (June-September) is the primary Indian growing season, so the
platform was blind exactly when advisory value is highest.

Radar penetrates cloud. v1 already fetched Sentinel-1 every night on a
30-day lookback and fed it to this (sar_soil_moisture.py, verbatim):

    index = 0.6 * vv_db + 0.4 * (vv_db - vh_db)

which used uncalibrated GRD DN, had no cited derivation for the weights,
returned dimensionless dB compared against thresholds as if it were
volumetric moisture, and produced NULL in 100% of rows.

v2 computes RVI (Radar Vegetation Index), a published dual-pol index that
tracks canopy biomass and is bounded [0, 1]:

    RVI = 4 * VH / (VV + VH)          (linear power, not dB)

RVI is NOT NDVI and is never written to ndvi_value. It is stored in its own
column with observation_source='sentinel-1' so the decision brain can weight
it appropriately and never confuse a radar proxy for an optical measurement.
"""

import numpy as np
from config import EPS, S1_MIN_VALID_PIXELS


def rvi_from_gamma0(vv: np.ndarray, vh: np.ndarray) -> dict:
    """
    vv, vh: linear-power gamma0 arrays (sentinel-1-rtc assets are already
    calibrated gamma0; do NOT pass dB).
    """
    vv = np.where(np.isfinite(vv) & (vv > 0), vv, np.nan)
    vh = np.where(np.isfinite(vh) & (vh > 0), vh, np.nan)

    rvi = 4.0 * vh / (vv + vh + EPS)
    rvi = np.clip(rvi, 0.0, 1.0)

    # Cross-pol ratio: complementary structure/moisture signal
    cr = vh / (vv + EPS)

    valid = np.isfinite(rvi)
    n = int(np.count_nonzero(valid))
    if n < S1_MIN_VALID_PIXELS:
        return {"rvi_mean": None, "rvi_std": None, "cross_ratio_db": None,
                "valid_pixels": n, "accepted": False,
                "reject_reason": f"valid_pixels {n} < {S1_MIN_VALID_PIXELS}"}

    return {
        "rvi_mean": round(float(np.nanmean(rvi)), 4),
        "rvi_std": round(float(np.nanstd(rvi)), 4),
        "cross_ratio_db": round(float(10.0 * np.log10(np.nanmean(cr) + EPS)), 3),
        "valid_pixels": n,
        "accepted": True,
        "reject_reason": None,
    }


# Approximate RVI -> NDVI-equivalent envelope.
# DELIBERATELY COARSE. RVI responds to canopy structure and dielectric
# properties, not chlorophyll; the relationship is crop- and
# incidence-angle-dependent. This mapping exists only to let the symbolic
# layer register "vegetation present / sparse / dense" during cloud outages.
# It must never drive a nutrient or disease diagnosis.
def rvi_vigor_class(rvi: float) -> str:
    if rvi is None:
        return "unknown"
    if rvi < 0.25:
        return "bare_or_very_sparse"
    if rvi < 0.45:
        return "sparse_canopy"
    if rvi < 0.65:
        return "developing_canopy"
    return "dense_canopy"
