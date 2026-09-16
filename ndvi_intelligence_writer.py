"""Persist the governed, observed NDVI intelligence record.

This module deliberately does NOT invent or train an ML model.  It promotes
only evidence already computed from an accepted Sentinel-2 observation into
ndvi_intelligence.  Estimated/predicted values require an independent
validation workflow and are therefore never produced here.
"""
from __future__ import annotations

from typing import Dict, Optional

from db import supabase, with_retry


def _num(value):
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def build_observed_intelligence(row: Dict) -> Optional[Dict]:
    """Build one observed-context intelligence record from one NDVI row."""
    if not row.get("land_id") or not row.get("tenant_id") or not row.get("scene_id"):
        return None
    observed = _num(row.get("ndvi_value"))
    if observed is None:
        return None

    meta = row.get("metadata") or {}
    ctx = meta.get("parcel_context") or {}
    evidence = meta.get("evidence") or {}
    quality = meta.get("quality_breakdown") or {}

    # Context is optional evidence. If it was not available, the record still
    # represents a valid observed satellite measurement, but not a contextual
    # comparison. Never fabricate a context value.
    context_present = bool(ctx.get("context_ndvi_mean") is not None)
    status = "observed_context" if context_present else "observed_context"

    return {
        "tenant_id": row["tenant_id"],
        "land_id": row["land_id"],
        "scene_id": row["scene_id"],
        "acquisition_date": row.get("acquisition_date"),
        "acquisition_time": row.get("acquisition_time"),
        "observed_ndvi": observed,
        "estimated_ndvi": None,
        "estimated_ndvi_low": None,
        "estimated_ndvi_high": None,
        "estimation_method": None,
        "model_version": None,
        "feature_version": "observed-context-v1",
        "context_ndvi_mean": _num(ctx.get("context_ndvi_mean")),
        "context_ndvi_median": _num(ctx.get("context_ndvi_median")),
        "context_ndvi_p10": _num(ctx.get("context_ndvi_p10")),
        "context_ndvi_p90": _num(ctx.get("context_ndvi_p90")),
        "context_ndvi_std": _num(ctx.get("context_ndvi_std")),
        "context_ndvi_mad": _num(ctx.get("context_ndvi_mad")),
        "parcel_context_delta": _num(ctx.get("parcel_context_delta")),
        "parcel_context_robust_z": _num(ctx.get("parcel_context_robust_z")),
        "context_effective_pixel_count": _num(ctx.get("context_effective_pixel_count")),
        "context_observed_fraction": _num(ctx.get("context_observed_fraction_of_target")),
        "context_purity": _num(ctx.get("context_purity")),
        "context_cloud_fraction": _num(ctx.get("context_cloud_fraction")),
        "context_shadow_fraction": _num(ctx.get("context_shadow_fraction")),
        "context_water_fraction": _num(ctx.get("context_water_fraction")),
        "spatial_anomaly_json": {
            "parcel_context_delta": _num(ctx.get("parcel_context_delta")),
            "parcel_context_robust_z": _num(ctx.get("parcel_context_robust_z")),
        },
        "uncertainty_json": {
            "scope": "observed measurement/context evidence; no predictive model",
            "measurement_quality": _num(row.get("quality_score")),
            "spatial_se": _num(row.get("ndvi_spatial_se")) or _num(evidence.get("ndvi_spatial_se")),
            "evidence_confidence": row.get("evidence_confidence"),
            "confidence_level": row.get("confidence_level"),
        },
        "evidence_json": {
            "observed_or_predicted": "observed",
            "measurement_status": row.get("measurement_status"),
            "spatial_stat_method": row.get("spatial_stat_method") or evidence.get("spatial_stat_method"),
            "effective_pixel_count": _num(row.get("effective_pixel_count")),
            "coverage_weighted_purity": _num(row.get("coverage_weighted_purity")),
            "boundary_contamination_fraction": _num(row.get("boundary_contamination_fraction")),
            "quality_breakdown": quality,
            "context_present": context_present,
        },
        "provenance_json": {
            "source": "ndvi_data",
            "observation_source": row.get("observation_source"),
            "observation_type": row.get("observation_type"),
            "scene_id": row.get("scene_id"),
            "pipeline_version": meta.get("pipeline_version"),
            "calibration": meta.get("calibration"),
            "derivation": "observed_context_promotion_only",
            "model_inference": False,
        },
        "intelligence_status": status,
        "validation_status": "not_applicable_observed",
        "validation_dataset_version": None,
        "observed_or_predicted": "observed",
    }


def persist_observed_intelligence(row: Dict) -> bool:
    record = build_observed_intelligence(row)
    if record is None:
        return False
    with_retry(
        lambda: supabase.table("ndvi_intelligence").upsert(
            record, on_conflict="tenant_id,land_id,scene_id"
        ).execute(),
        what=f"upsert observed intelligence {record['land_id']}/{record['scene_id']}",
        attempts=3,
    )
    return True
