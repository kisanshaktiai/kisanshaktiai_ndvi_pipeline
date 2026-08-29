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

v2 computes RVI (Radar Vegetation Index), the dual-pol form
(RVI = 4*sigma0_VH / (sigma0_VV + sigma0_VH), Mandal et al. 2020, RSE 247)
in linear power. Its mathematical range is [0, 2] (maximum when VH == VV);
v2.1 clipped it to [0, 1], which truncated every pixel with VH/VV > 1/3 -
i.e. exactly the dense-canopy pixels the index exists to detect (F-9).

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
    rvi = np.clip(rvi, 0.0, 2.0)

    # Cross-pol ratio: complementary structure/moisture signal
    cr = vh / (vv + EPS)

    valid = np.isfinite(rvi) & np.isfinite(cr)
    n = int(np.count_nonzero(valid))
    if n < S1_MIN_VALID_PIXELS:
        return {"rvi_mean": None, "rvi_std": None, "cross_ratio_db": None,
                "valid_pixels": n, "accepted": False,
                "reject_reason": f"valid_pixels {n} < {S1_MIN_VALID_PIXELS}"}

    return {
        "rvi_mean": round(float(np.nanmean(rvi[valid])), 4),
        "rvi_std": round(float(np.nanstd(rvi[valid])), 4),
        "cross_ratio_db": round(float(10.0 * np.log10(np.nanmean(cr[valid]) + EPS)), 3),
        "vv_db_mean": round(float(10.0 * np.log10(np.nanmean(vv[valid]) + EPS)), 3),
        "vh_db_mean": round(float(10.0 * np.log10(np.nanmean(vh[valid]) + EPS)), 3),
        "valid_pixels": n,
        "accepted": True,
        "reject_reason": None,
    }
