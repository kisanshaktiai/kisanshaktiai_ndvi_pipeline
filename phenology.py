"""
phenology.py — stage-relative NDVI interpretation.

COMPLETELY REWRITTEN. The previous version of this file was wrong twice over,
and both errors explain the shape of this one.

  MISTAKE 1  It carried a hardcoded CROP_NDVI_PHENOLOGY dict with an invented
             stage vocabulary ('squaring_flowering', 'bulbing', crop 'TUR').
             ZERO of those 54 stage codes matched crop_stage_master, which
             holds 231 canonical stages the whole platform validates against.

  MISTAKE 2  It assumed every crop counts days from SOWING. The platform
             models das_reference as sowing | transplanting | planting |
             nursery_sowing. Onion runs a 0-55 day nursery before its DAT
             clock starts; chilli and tomato 0-30; transplanted rice 0-24;
             potato counts from planting; maize from emergence. Feeding
             days-since-sowing into an onion envelope indexed on DAT is off
             by up to 55 days - three growth stages - and would report a
             healthy crop as failing, with full confidence.

THIS VERSION DELEGATES. It calls public.ndvi_stage_anomaly(), which wraps
resolve_crop_phenology() (resolver_version 9). That engine already handles
clock selection, variety das_min/max overrides, the biological transition
ledger, evidence-driven transitions, phenology_index and confidence.

Nothing here duplicates stage logic. If the platform's understanding of
phenology changes, this file inherits it automatically.
"""

from typing import Optional, List, Dict, Any
from datetime import date

from db import get_supabase_client
from logger import logger


# ---------------------------------------------------------------------------
# CROP CODE RESOLUTION  (audit finding P-25)
# ---------------------------------------------------------------------------
# lands.current_crop holds LOCALISED DISPLAY NAMES, not codes. Live values:
#   Groundnut | pulses | rice | Rice | sugarcane | Sugarcane | wheat
#   | ऊस | गहू | तांदूळ | राजमा
# crop_stage_master.crop_code is lowercase English.
#   direct lower() match ............ 6 of 10 resolve
#   via crops.value/label/label_mr ... 8 of 10 resolve
#   never resolve ................... 'pulses' (a crop GROUP, not a crop),
#                                     'तांदूळ' (Marathi for rice, unmapped)
# Only 1 of 29 active lands has current_crop_id set, so free text is the de
# facto source and this lookup is mandatory.
# ---------------------------------------------------------------------------
_crop_cache: Dict[str, Optional[str]] = {}


def resolve_crop_code(raw: Optional[str]) -> Optional[str]:
    """
    Map a display name to a canonical crop_code via public.crops.
    Returns None when it cannot resolve - NEVER guesses.
    """
    if not raw:
        return None
    key = str(raw).strip()
    if key in _crop_cache:
        return _crop_cache[key]

    sb = get_supabase_client()
    result = None
    try:
        rows = (sb.table("crops")
                  .select("value,label,label_mr,label_hi")
                  .limit(2000).execute().data) or []
        low = key.lower()
        for r in rows:
            if (str(r.get("value") or "").lower() == low
                    or str(r.get("label") or "").lower() == low
                    or r.get("label_mr") == key
                    or r.get("label_hi") == key):
                result = (r.get("value") or "").lower() or None
                break
    except Exception:
        logger.exception(f"crop code lookup failed for {key!r}")

    if result is None:
        logger.warning(f"Crop {key!r} does not resolve to a canonical crop_code")
    _crop_cache[key] = result
    return result


# ---------------------------------------------------------------------------
# CROP CYCLE  (audit finding P-24 - OPEN PLATFORM BUG, workaround below)
# ---------------------------------------------------------------------------
# resolve_crop_phenology does NOT treat crop_cycle='universal' as a wildcard,
# but fn_resolve_stage does. Verified live:
#   crop_schedules.crop_cycle    -> only value present is 'plant'
#   crop_stage_master.crop_cycle -> 'universal' for every crop but sugarcane
#
#   resolve_crop_phenology('rice','plant',...)      -> NO ROWS
#   resolve_crop_phenology('rice',NULL,...)         -> RICE_TILLERING
#   resolve_crop_phenology('sugarcane','plant',...) -> SUGARCANE_GRAND_GROWTH
#   resolve_crop_phenology('sugarcane','universal') -> NO ROWS
#
# No single argument works for all crops. Passing the land's real value
# silently returns nothing for everything except sugarcane.
#
# WORKAROUND: pass the real cycle only for sugarcane (where plant vs ratoon
# genuinely differ AND the real value works); NULL everywhere else.
# REMOVE THIS once migration 003 section 5 BUG A is applied.
# ---------------------------------------------------------------------------
CYCLE_AWARE_CROPS = {"sugarcane"}


def effective_crop_cycle(crop_code: Optional[str],
                         raw_cycle: Optional[str]) -> Optional[str]:
    if crop_code and crop_code.lower() in CYCLE_AWARE_CROPS:
        return raw_cycle
    return None


# ---------------------------------------------------------------------------
# ANOMALY - delegated
# ---------------------------------------------------------------------------
def compute_anomaly(ndvi: float,
                    crop_raw: Optional[str],
                    cultivation_method: Optional[str],
                    sow_date: Optional[date],
                    transplant_date: Optional[date] = None,
                    crop_cycle: Optional[str] = None,
                    variety_id: Optional[str] = None,
                    current_gdd: Optional[float] = None,
                    land_id: Optional[str] = None,
                    as_of: Optional[date] = None) -> Dict[str, Any]:
    """
    Stage-relative NDVI anomaly via public.ndvi_stage_anomaly().

    Returns the RPC row, or a refusal dict. NEVER falls back to an absolute
    threshold - that is the C-11 defect which classified healthy rice at
    12 DAT as CRITICAL.

    The returned ndvi_confidence is bounded by stage_confidence: an anomaly
    cannot be more certain than the stage it is measured against. Every land
    currently resolves at das_provisional = 0.50.
    """
    unknown = {"status": "unknown", "z_score": None, "stage_code": None,
               "ndvi_confidence": 0.0, "reason": None}

    crop_code = resolve_crop_code(crop_raw)
    if crop_code is None:
        return {**unknown, "reason": f"crop_unresolved:{crop_raw}"}
    if not cultivation_method:
        return {**unknown, "reason": "cultivation_method_required"}
    if not sow_date:
        return {**unknown, "reason": "sow_date_required"}

    def _iso(d):
        return d.isoformat() if hasattr(d, "isoformat") else d

    try:
        rows = get_supabase_client().rpc("ndvi_stage_anomaly", {
            "p_ndvi": float(ndvi),
            "p_crop_code": crop_code,
            "p_cultivation_method": cultivation_method,
            "p_sow_date": _iso(sow_date),
            "p_transplant_date": _iso(transplant_date),
            "p_crop_cycle": effective_crop_cycle(crop_code, crop_cycle),
            "p_variety_id": variety_id,
            "p_current_gdd": current_gdd,
            "p_as_of": _iso(as_of or date.today()),
            "p_land_id": land_id,
        }).execute().data or []
    except Exception:
        logger.exception(f"ndvi_stage_anomaly RPC failed for land {land_id}")
        return {**unknown, "reason": "rpc_error"}

    if not rows:
        return {**unknown, "reason": "stage_unresolved"}

    r = rows[0]
    r.setdefault("ndvi_confidence", 0.0)
    return r


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
