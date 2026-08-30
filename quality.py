"""
quality.py - per-acquisition quality scoring.

v1 DEFECT FIXED (P-09, P-13, C-13):
  * quality_score was NULL in 2,477 of 2,481 rows - the column existed and
    was never written.
  * The only populated quality-adjacent metric, coverage_percentage, was
    valid_pixels / BOUNDING BOX size. That conflates polygon-in-bbox geometry
    (a diagonal field scores ~50% before any data issue), SCL mask survival,
    and genuine cloud loss. It is not a quality metric.
  * An aggregation from 4 valid pixels was stored indistinguishably from one
    over 2,054 pixels.

v2 computes an explicit, decomposable score over the BUFFERED FIELD and
refuses to persist anything below MIN_QUALITY_SCORE.

v3 (smallholder evidence audit) replaces PIXEL COUNT with EFFECTIVE PIXEL
COUNT everywhere it was used as a proxy for information support, and adds
the evidence tier the decision layer is meant to gate on.

Why: eight cells each 25 % inside the field is EPC 2.0, not 8. The v2 score
could not tell that apart from eight whole interior pixels, so two very
different observations received the same trust. EPC and purity separate
them; the blunt "< 0.25 acre -> x0.65" rule is demoted to a fallback that
only fires when purity cannot be computed.

Three numbers stay deliberately separate and must not be collapsed:
    quality_score    - how good is this MEASUREMENT (imagery property)
    confidence_score - how far may a DECISION lean on it (inference)
    evidence_tier    - which EPC band the spatial support falls in
"""

from dataclasses import dataclass, asdict
from typing import Optional
import math
import numpy as np

from config import (
    MIN_VALID_PIXELS, MIN_VALID_FRACTION, MIN_QUALITY_SCORE,
    QW_COVERAGE, QW_PIXELCOUNT, QW_CLOUDFREE,
    MICRO_LAND_ACRES, MICRO_LAND_FACTOR, GEOMETRY_CONFIDENCE_FACTOR,
    MAX_FIELD_CLOUD_FRACTION, MAX_FIELD_SHADOW_FRACTION, MAX_FIELD_SNOW_FRACTION,
    EPC_STRONG, EPC_LIMITED, EPC_WEAK, MIN_EPC, EPC_SATURATION,
    LOW_PURITY_THRESHOLD, LOW_PURITY_FACTOR,
)


def evidence_tier(epc: float) -> tuple:
    """
    (measurement_status, evidence_confidence) from spatial support alone.

    EPC >= EPC_STRONG is the only published anchor (Sitokonstantinou et al.
    2020: >= 8 full pixels for Sentinel-2 field monitoring). The lower
    boundaries are engineering operating rules, NOT validated constants -
    see config.py. They must be re-derived from the smallholder calibration
    dataset before anyone quotes them as accuracy.
    """
    if epc is None:
        return "UNKNOWN_SPATIAL_SUPPORT", "low"
    if epc >= EPC_STRONG:
        return "OBSERVED_STRONG", "high"
    if epc >= EPC_LIMITED:
        return "OBSERVED_LIMITED", "medium"
    if epc >= EPC_WEAK:
        return "OBSERVED_WEAK", "low"
    return "INSUFFICIENT_SPATIAL_SUPPORT", "insufficient"


@dataclass
class QualityAssessment:
    quality_score: float
    confidence_score: float
    confidence_level: str              # high | medium | low | rejected
    accepted: bool
    reject_reason: Optional[str]
    valid_pixels: int
    field_pixels: int
    valid_fraction: float
    cloud_fraction: float
    shadow_fraction: float
    water_fraction: float
    buffer_applied: bool
    q_coverage: float
    q_support: float
    q_cloudfree: float
    epc_total: float
    epc_valid: float
    purity: float
    boundary_share: float
    interior_share: float
    n_eff: float
    measurement_status: str
    evidence_confidence: str
    geometry_confidence: str
    micro_land: bool
    snow_fraction: float
    saturated_fraction: float
    unaccounted_fraction: float

    def to_dict(self):
        return asdict(self)


def assess(masks: dict,
           buffer_applied: bool,
           area_acres: float | None = None,
           geometry_confidence: str = "high",
           stats: dict | None = None) -> QualityAssessment:
    """
    stats : the coverage-weighted NDVI statistics from
            indices.weighted_index_statistics(). Supplies epc / purity /
            n_eff, i.e. the real spatial support. When absent the function
            degrades to the v2 pixel-count behaviour so the module stays
            usable standalone.
    """
    field_px = masks["n_field_pixels"]
    valid_px = masks["n_crop_pixels"]

    # AREA-weighted validity (share of field area, not of touched cells).
    epc_total = float(masks.get("epc_total") or 0.0)
    epc_valid = float((stats or {}).get("epc") or masks.get("epc_crop") or 0.0)
    valid_fraction = (epc_valid / epc_total) if epc_total > 0 else (
        (valid_px / field_px) if field_px else 0.0)

    purity = (stats or {}).get("purity")
    interior_share = (stats or {}).get("interior_share")
    boundary_share = (stats or {}).get("boundary_share")
    n_eff = (stats or {}).get("n_eff")

    q_coverage = min(valid_fraction / MIN_VALID_FRACTION, 1.0) if MIN_VALID_FRACTION else 0.0
    # Support term is now EPC-based: eight quarter-covered cells score as
    # the 2.0 effective pixels they are.
    q_support = min(epc_valid / EPC_SATURATION, 1.0) if epc_valid else 0.0
    q_cloudfree = max(0.0, 1.0 - (masks["cloud_fraction"] + masks["shadow_fraction"]))

    score = (QW_COVERAGE * q_coverage
             + QW_PIXELCOUNT * q_support
             + QW_CLOUDFREE * q_cloudfree)

    # An unbuffered small field is measurably noisier: penalise rather than
    # silently treating it as equivalent.
    if not buffer_applied:
        score *= 0.80

    # ---- QUALITY vs CONFIDENCE SPLIT ----------------------------------
    # quality_score  = "how good is this MEASUREMENT?"  (imagery property)
    # confidence_score = "how far may a DECISION lean on it?" (inference)
    # A clean measurement over a 0.23-acre field located by a centroid guess
    # is HIGH quality and LOW confidence. Collapsing the two hides exactly
    # the case that dominates smallholder farming.
    quality = round(min(max(score, 0.0), 1.0), 3)

    # ---- DECISION-CONFIDENCE DISCOUNTS --------------------------------
    # ---- MICRO-LAND PENALTY -------------------------------------------
    # Adopted from the retired engine repo (land_ndvi.ndvi_confidence_score),
    # which was sharper than my original design on exactly the case that
    # dominates this tenant: 29% of fields are under 0.5 acre and the
    # smallest is 0.23 acre (~930 m2, ~9 Sentinel-2 pixels).
    #
    # At that size essentially every pixel is an edge pixel and the point
    # spread function mixes crop with bunds, tracks and neighbouring fields.
    # The measurement is not wrong, but it is meaningfully less trustworthy,
    # and that must be visible downstream rather than averaged away.
    # v3: purity is the direct measurement of the effect the micro-land rule
    # was proxying for, so it takes precedence. The area rule now fires only
    # when purity is unavailable - otherwise a compact 0.2-acre field with
    # EPC 8.2 would be punished as hard as a narrow strip with EPC 3.1.
    micro_land = area_acres is not None and area_acres < MICRO_LAND_ACRES
    if purity is not None:
        if purity < LOW_PURITY_THRESHOLD:
            score *= LOW_PURITY_FACTOR
    elif micro_land:
        score *= MICRO_LAND_FACTOR

    # ---- GEOMETRY CONFIDENCE ------------------------------------------
    # Also from the engine repo (land_geometry.resolve_land_geometry).
    # A 40 m buffer around a centroid is a guess at where the field is;
    # it should never carry the same weight as a surveyed polygon.
    score *= GEOMETRY_CONFIDENCE_FACTOR.get(geometry_confidence, 0.5)

    # ---- SPATIAL SUPPORT TIER ------------------------------------------
    status, ev_conf = evidence_tier(epc_valid if epc_valid else None)
    # Weak support caps decision confidence outright; it is not averaged
    # away against a clean sky.
    if ev_conf == "medium":
        score = min(score, 0.74)
    elif ev_conf == "low":
        score = min(score, 0.54)
    elif ev_conf == "insufficient":
        score = min(score, 0.30)

    # F-2 GUARD. ndvi_data.quality_score is float4 (real) in the live schema
    # while confidence_score is numeric; the CHECK compares them with a
    # 1e-9 tolerance. float4(0.704) == 0.703999996..., so an equal 3-dp pair
    # violates the constraint and the whole land fails. Bound confidence by
    # the float4 representation of quality, strictly below it.
    q32 = float(np.float32(quality))
    confidence = math.floor((min(max(score, 0.0), q32) - 1e-6) * 1e6) / 1e6
    confidence = max(confidence, 0.0)

    reject = None
    # FIELD-LEVEL image-quality gates. This is "reject scenes exceeding the
    # configured cloud threshold" done correctly: evaluated over the field,
    # never over the 290x110 km granule (that mistake produced a 100% skip
    # rate for 29 consecutive days).
    if masks["cloud_fraction"] > MAX_FIELD_CLOUD_FRACTION:
        reject = f"field cloud {masks['cloud_fraction']:.0%} > {MAX_FIELD_CLOUD_FRACTION:.0%}"
    elif masks["shadow_fraction"] > MAX_FIELD_SHADOW_FRACTION:
        reject = f"field shadow {masks['shadow_fraction']:.0%} > {MAX_FIELD_SHADOW_FRACTION:.0%}"
    elif masks.get("snow_fraction", 0.0) > MAX_FIELD_SNOW_FRACTION:
        reject = f"field snow/bright-cloud {masks['snow_fraction']:.0%} > {MAX_FIELD_SNOW_FRACTION:.0%}"
    elif epc_valid and epc_valid < MIN_EPC:
        reject = (f"effective_pixel_count {epc_valid:.2f} < {MIN_EPC} "
                  f"({valid_px} cells, purity {purity if purity is not None else float('nan'):.2f})")
    elif not epc_valid and valid_px < MIN_VALID_PIXELS:
        reject = f"valid_pixels {valid_px} < {MIN_VALID_PIXELS}"
    elif valid_fraction < MIN_VALID_FRACTION:
        reject = f"valid_fraction {valid_fraction:.2f} < {MIN_VALID_FRACTION}"
    elif quality < MIN_QUALITY_SCORE:
        reject = f"quality_score {quality:.2f} < {MIN_QUALITY_SCORE}"

    # confidence_level describes DECISION trust, so it keys off
    # confidence_score, not quality_score.
    if reject:
        level = "rejected"
    elif confidence >= 0.75:
        level = "high"
    elif confidence >= 0.55:
        level = "medium"
    else:
        level = "low"

    return QualityAssessment(
        quality_score=quality,
        confidence_score=confidence,
        confidence_level=level,
        accepted=reject is None,
        reject_reason=reject,
        valid_pixels=valid_px,
        field_pixels=field_px,
        valid_fraction=round(valid_fraction, 4),
        cloud_fraction=round(masks["cloud_fraction"], 4),
        shadow_fraction=round(masks["shadow_fraction"], 4),
        water_fraction=round(masks["water_fraction"], 4),
        buffer_applied=buffer_applied,
        q_coverage=round(q_coverage, 3),
        q_support=round(q_support, 3),
        q_cloudfree=round(q_cloudfree, 3),
        epc_total=round(epc_total, 4),
        epc_valid=round(epc_valid, 4),
        purity=purity,
        boundary_share=boundary_share,
        interior_share=interior_share,
        n_eff=n_eff,
        measurement_status=("rejected" if reject else status),
        evidence_confidence=("rejected" if reject else ev_conf),
        geometry_confidence=geometry_confidence,
        micro_land=micro_land,
        snow_fraction=round(masks.get("snow_fraction", 0.0), 4),
        saturated_fraction=round(masks.get("saturated_fraction", 0.0), 4),
        unaccounted_fraction=round(masks.get("unaccounted_fraction", 0.0), 4),
    )
