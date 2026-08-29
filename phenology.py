"""
phenology.py - temporal helpers over TRUE acquisition dates.

v2.2: compute_anomaly() / resolve_crop_code() removed. They called
public.ndvi_stage_anomaly with land.cultivation_method and land.sowing_date,
neither of which exists on public.lands, so the RPC was never reached and
the result was never persisted (audit finding F-11). Stage-relative
interpretation belongs to the decision layer, which owns the phenology
resolver (fn_effective_method / resolve_crop_phenology_for_land).
"""

from typing import Optional, List, Dict, Any
from datetime import date

from logger import logger


# ---------------------------------------------------------------------------
# TREND - over TRUE acquisition dates
# ---------------------------------------------------------------------------
def classify_trend(series: List[tuple], window_days: int = 21) -> dict:
    """
    series: [(acquisition_date, ndvi), ...] - REAL acquisitions only.

    v1 fitted np.polyfit against np.arange(len(values)) - an index, not a
    date - over a series whose dates were the pipeline RUN date and which was
    72% forward-filled duplicates. Slope units were uninterpretable and a
    7-day window compared a value against a copy of itself.

    Returns slope in NDVI units per DAY.
    """
    import numpy as np

    pts = sorted([(d, v) for d, v in series if v is not None], key=lambda x: x[0])
    if len(pts) < 3:
        return {"slope_per_day": None, "direction": "insufficient_data",
                "n_points": len(pts), "span_days": None, "delta_total": None}

    end = pts[-1][0]
    win = [(d, v) for d, v in pts if (end - d).days <= window_days]
    if len(win) < 3:
        win = pts[-3:]

    t0 = win[0][0]
    x = np.array([(d - t0).days for d, _ in win], dtype="float64")
    y = np.array([v for _, v in win], dtype="float64")
    span = float(x[-1] - x[0])
    if span <= 0:
        return {"slope_per_day": None, "direction": "insufficient_data",
                "n_points": len(win), "span_days": 0, "delta_total": None}

    slope = float(np.polyfit(x, y, 1)[0])
    if slope <= -0.010:
        direction = "sharp_decline"
    elif slope <= -0.003:
        direction = "declining"
    elif slope < 0.003:
        direction = "stable"
    elif slope < 0.010:
        direction = "improving"
    else:
        direction = "sharp_improving"

    return {"slope_per_day": round(slope, 5), "direction": direction,
            "n_points": len(win), "span_days": span,
            "delta_total": round(float(y[-1] - y[0]), 4)}


# ---------------------------------------------------------------------------
# HISTOGRAM ANALYTICS
# ---------------------------------------------------------------------------
def histogram_stats(hist: Optional[dict]) -> Optional[dict]:
    """
    Derive percentiles and uniformity from a stored per-field histogram.

    This is why the histogram earns its ~200 bytes: uniformity_cv is the
    single most useful discriminator NDVI offers a diagnosis -
        uniform low  -> whole-field cause (nutrient, water)
        patchy  low  -> localised cause (pest, disease, soil variability)
    and it stays recomputable from stored data without touching imagery again.
    """
    if not hist:
        return None
    bins, counts = hist.get("bins"), hist.get("counts")
    if not bins or not counts or len(bins) != len(counts) + 1:
        return None
    total = sum(counts)
    if total == 0:
        return None

    mids = [(bins[i] + bins[i + 1]) / 2 for i in range(len(counts))]
    mean = sum(m * c for m, c in zip(mids, counts)) / total
    var = sum(c * (m - mean) ** 2 for m, c in zip(mids, counts)) / total
    std = var ** 0.5

    def pct(p):
        target, run = total * p, 0
        for m, c in zip(mids, counts):
            run += c
            if run >= target:
                return m
        return mids[-1]

    return {
        "mean": round(mean, 4),
        "std": round(std, 4),
        "p10": round(pct(0.10), 4),
        "p50": round(pct(0.50), 4),
        "p90": round(pct(0.90), 4),
        "uniformity_cv": round(std / (abs(mean) + 1e-6), 4),
        "pixels": total,
    }
