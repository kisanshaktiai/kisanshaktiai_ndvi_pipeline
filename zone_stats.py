"""
zone_stats.py - "which part of the field is behind" from the data the pipeline
already holds at write time. No new imagery, no model.

WHAT THIS IS
-----------
For every optical pass the processor has the NDVI / NDRE / NDMI arrays on the
10 m grid and the coverage weight of every cell. This module splits the field
into four quarters around the coverage-weighted centre (north-east, north-west,
south-east, south-west - the words a farmer uses standing on the bund) and
computes, per quarter, the coverage-weighted mean of each index, the number of
interior cells (coverage >= 0.99, crop class) and the effective pixel count.

It then names the WEAKEST quarter by NDVI and asks two honest questions:
  1. Is the gap real?  quarter mean must sit >= 2 x the field's spatial
     standard error below the field median AND at least ZONE_MIN_DELTA below
     it (an engineering noise floor for Sentinel-2 NDVI; NOT a validated
     agronomic threshold - labelled as such in the output).
  2. Is it persistent?  the same quarter was the weakest on the previous
     clear pass (persistence is what separates a real patch from cloud edge,
     shadow or a wet corner on one day).

Level:  none | watch (real gap, one pass) | check (real gap, two passes).
Field gates: >= ZONE_MIN_INTERIOR_CELLS interior cells in the field and
>= 2 in the quarter, evidence tier medium or better. Below that the field is
too small for a quarter to mean anything and the output says so
(level 'none', reason 'field_too_small') - a 10-guntha plot gets no zone.

WHAT THIS IS NOT
----------------
It does not say WHY. NDVI falling in one corner is compatible with water
shortage, nutrient shortage, lodging, a pest or a wet patch. The 'pattern'
field is a HINT for the decision layer (which index moved with NDVI), never
a diagnosis: the farmer's photo of that corner is the confirmation.
"""
from __future__ import annotations

from typing import Dict, Optional

import numpy as np

ZONE_MIN_INTERIOR_CELLS = 4      # field must have >= 4 fully interior cells (2x2) - engineering rule
ZONE_MIN_QUARTER_CELLS = 2       # a quarter needs >= 2 interior cells to be judged
ZONE_MIN_DELTA = 0.05            # NDVI noise floor between quarters - engineering rule, NOT validated
ZONE_SE_MULTIPLE = 2.0           # gap must exceed 2 x spatial SE of the field
QUARTERS = ("NE", "NW", "SE", "SW")


def _wmean(arr: Optional[np.ndarray], w: np.ndarray, sel: np.ndarray) -> Optional[float]:
    if arr is None:
        return None
    m = sel & np.isfinite(arr) & (w > 0)
    if not m.any():
        return None
    ww = w[m]
    return float(np.sum(arr[m] * ww) / np.sum(ww))


def compute_zones(idx: Dict[str, Optional[np.ndarray]], crop_w: np.ndarray, coverage: np.ndarray,
                  crop_mask: np.ndarray, field_median_ndvi: Optional[float],
                  spatial_se: Optional[float], evidence_confidence: Optional[str],
                  previous_zones: Optional[dict]) -> dict:
    ndvi = idx.get("NDVI")
    out = {
        "method": "quarter_split_coverage_weighted_v1",
        "split": "coverage-weighted centre; rows south, cols east (north-up UTM grid)",
        "engineering_rules": {"min_interior_cells": ZONE_MIN_INTERIOR_CELLS, "min_quarter_cells": ZONE_MIN_QUARTER_CELLS,
                              "min_delta": ZONE_MIN_DELTA, "se_multiple": ZONE_SE_MULTIPLE,
                              "note": "operating rules, not validated agronomic thresholds"},
        "quarters": {}, "weakest": None, "delta": None, "level": "none", "reason": None,
        "persistent": False, "pattern": None,
        # same test on the moisture signal (NDMI): which quarter is driest, is the gap real, is it persistent
        "water": {"weakest": None, "delta": None, "level": "none", "persistent": False},
    }
    if ndvi is None or crop_w is None or not np.isfinite(ndvi).any():
        out["reason"] = "no_ndvi"
        return out

    interior = (coverage >= 0.99) & crop_mask & np.isfinite(ndvi)
    n_interior = int(interior.sum())
    if n_interior < ZONE_MIN_INTERIOR_CELLS:
        out["reason"] = "field_too_small"
        out["interior_cells"] = n_interior
        return out
    if (evidence_confidence or "").lower() not in ("high", "medium"):
        out["reason"] = "evidence_below_medium"
        out["interior_cells"] = n_interior
        return out

    rows, cols = np.indices(ndvi.shape)
    wsum = float(crop_w.sum())
    if wsum <= 0:
        out["reason"] = "no_crop_weight"
        return out
    r0 = float((rows * crop_w).sum() / wsum)
    c0 = float((cols * crop_w).sum() / wsum)
    north = rows < r0
    east = cols >= c0
    sel = {"NE": north & east, "NW": north & ~east, "SE": ~north & east, "SW": ~north & ~east}

    quarters = {}
    for q in QUARTERS:
        s = sel[q]
        q_int = int((interior & s).sum())
        quarters[q] = {
            "interior_cells": q_int,
            "epc": round(float(crop_w[s].sum()), 3),
            "ndvi": None if q_int < ZONE_MIN_QUARTER_CELLS else _wmean(ndvi, crop_w, s),
            "ndre": None if q_int < ZONE_MIN_QUARTER_CELLS else _wmean(idx.get("NDRE"), crop_w, s),
            "ndmi": None if q_int < ZONE_MIN_QUARTER_CELLS else _wmean(idx.get("NDMI"), crop_w, s),
        }
    out["quarters"] = {q: {k: (round(v, 4) if isinstance(v, float) else v) for k, v in d.items()} for q, d in quarters.items()}
    out["interior_cells"] = n_interior

    judged = {q: d for q, d in quarters.items() if d["ndvi"] is not None}

    # Water stress is judged INDEPENDENTLY of the growth gap: an evenly grown
    # field can still have one corner drying out first.
    # ---- water: the driest quarter by NDMI, judged the same way -----------------
    ndmi_vals = {q: d["ndmi"] for q, d in judged.items() if d.get("ndmi") is not None}
    if len(ndmi_vals) >= 2:
        w_weak = min(ndmi_vals, key=ndmi_vals.get)
        w_med = float(np.median(list(ndmi_vals.values())))
        w_delta = float(w_med - ndmi_vals[w_weak])
        w_real = w_delta >= ZONE_MIN_DELTA
        pw = ((previous_zones or {}).get("water") or {}) if isinstance(previous_zones, dict) else {}
        w_persist = bool(pw.get("level") in ("watch", "check") and pw.get("weakest") == w_weak)
        out["water"] = {"weakest": w_weak if w_real else None, "delta": round(w_delta, 4),
                        "level": ("check" if w_persist else "watch") if w_real else "none", "persistent": w_real and w_persist}

    if len(judged) < 2 or field_median_ndvi is None:
        out["reason"] = "too_few_judged_quarters"
        return out
    weakest = min(judged, key=lambda q: judged[q]["ndvi"])
    delta = float(field_median_ndvi - judged[weakest]["ndvi"])
    out["weakest"] = weakest
    out["delta"] = round(delta, 4)

    se = float(spatial_se) if spatial_se not in (None, 0) else None
    real_gap = delta >= ZONE_MIN_DELTA and (se is None or delta >= ZONE_SE_MULTIPLE * se)
    if not real_gap:
        out["reason"] = "gap_within_noise"
        return out

    prev_weakest = (previous_zones or {}).get("weakest") if isinstance(previous_zones, dict) else None
    prev_real = bool((previous_zones or {}).get("level") in ("watch", "check")) if isinstance(previous_zones, dict) else False
    persistent = prev_real and prev_weakest == weakest
    out["persistent"] = persistent
    out["level"] = "check" if persistent else "watch"
    out["reason"] = "gap_persists_two_passes" if persistent else "gap_one_pass"

    # pattern hint: which other index is also lowest in that quarter (a hint for TARKA, not a cause)
    def lowest(key: str) -> Optional[str]:
        vals = {q: d[key] for q, d in judged.items() if d.get(key) is not None}
        return min(vals, key=vals.get) if len(vals) >= 2 else None
    lo_ndmi, lo_ndre = lowest("ndmi"), lowest("ndre")
    if lo_ndmi == weakest and lo_ndre != weakest:
        out["pattern"] = "moisture_led"       # colour and water both lowest here
    elif lo_ndre == weakest and lo_ndmi != weakest:
        out["pattern"] = "greenness_led"      # chlorophyll signal lowest, water not
    elif lo_ndmi == weakest and lo_ndre == weakest:
        out["pattern"] = "all_indices"
    else:
        out["pattern"] = "vigour_only"
    return out
