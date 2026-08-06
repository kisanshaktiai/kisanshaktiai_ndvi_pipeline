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
"""

from dataclasses import dataclass, asdict
from typing import Optional

from config import (
    MIN_VALID_PIXELS, MIN_VALID_FRACTION, MIN_QUALITY_SCORE,
    QW_COVERAGE, QW_PIXELCOUNT, QW_CLOUDFREE, QUALITY_SATURATION_PIXELS,
    MICRO_LAND_ACRES, MICRO_LAND_FACTOR, GEOMETRY_CONFIDENCE_FACTOR,
    MAX_FIELD_CLOUD_FRACTION, MAX_FIELD_SHADOW_FRACTION, MAX_FIELD_SNOW_FRACTION,
)


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
    q_pixelcount: float
    q_cloudfree: float
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
           geometry_confidence: str = "high") -> QualityAssessment:
    """
    area_acres / geometry_confidence adopted from the retired
    kisanshakti-ndvi-engine repo. See the penalty blocks below.
    """
    field_px = masks["n_field_pixels"]
    valid_px = masks["n_crop_pixels"]
    valid_fraction = (valid_px / field_px) if field_px else 0.0

    q_coverage = min(valid_fraction / MIN_VALID_FRACTION, 1.0) if MIN_VALID_FRACTION else 0.0
    q_pixelcount = min(valid_px / QUALITY_SATURATION_PIXELS, 1.0)
    q_cloudfree = max(0.0, 1.0 - (masks["cloud_fraction"] + masks["shadow_fraction"]))

    score = (QW_COVERAGE * q_coverage
             + QW_PIXELCOUNT * q_pixelcount
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
    micro_land = area_acres is not None and area_acres < MICRO_LAND_ACRES
    if micro_land:
        score *= MICRO_LAND_FACTOR

    # ---- GEOMETRY CONFIDENCE ------------------------------------------
    # Also from the engine repo (land_geometry.resolve_land_geometry).
    # A 40 m buffer around a centroid is a guess at where the field is;
    # it should never carry the same weight as a surveyed polygon.
    score *= GEOMETRY_CONFIDENCE_FACTOR.get(geometry_confidence, 0.5)

    confidence = round(min(max(score, 0.0), quality), 3)

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
    elif valid_px < MIN_VALID_PIXELS:
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
        q_pixelcount=round(q_pixelcount, 3),
        q_cloudfree=round(q_cloudfree, 3),
        geometry_confidence=geometry_confidence,
        micro_land=micro_land,
        snow_fraction=round(masks.get("snow_fraction", 0.0), 4),
        saturated_fraction=round(masks.get("saturated_fraction", 0.0), 4),
        unaccounted_fraction=round(masks.get("unaccounted_fraction", 0.0), 4),
    )
