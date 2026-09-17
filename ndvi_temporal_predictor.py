"""Temporal NDVI forecasting from real historical observations.

This is a conservative forecasting layer, not a remote-sensing reconstruction
model. It uses only observations already accepted by the measurement pipeline.
Predictions are explicitly marked estimated_unvalidated and include an
uncertainty interval. No predicted value is ever written as observed/validated.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from math import sqrt
from typing import Dict, List, Optional, Tuple

from db import supabase, with_retry, log_step

MODEL_VERSION = "ndvi-temporal-v1"
FEATURE_VERSION = "temporal-features-v1"
MIN_HISTORY = 3
MAX_HISTORY = 12
MAX_FORECAST_DAYS = 5
MAX_GAP_DAYS = 12
MIN_QUALITY = 0.55
MIN_PREDICTION = -1.0
MAX_PREDICTION = 1.0


def _f(v):
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _date(v):
    if not v:
        return None
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).date()
    except ValueError:
        try:
            return datetime.strptime(str(v)[:10], "%Y-%m-%d").date()
        except ValueError:
            return None


def _weighted_linear_regression(points: List[Tuple[float, float, float]]):
    """Return intercept, slope, residual RMSE for (x,y,weight)."""
    sw = sx = sy = sxx = sxy = 0.0
    for x, y, w in points:
        sw += w
        sx += w * x
        sy += w * y
        sxx += w * x * x
        sxy += w * x * y
    den = sw * sxx - sx * sx
    if sw <= 0 or abs(den) < 1e-12:
        return None
    slope = (sw * sxy - sx * sy) / den
    intercept = (sy - slope * sx) / sw
    err = 0.0
    for x, y, w in points:
        r = y - (intercept + slope * x)
        err += w * r * r
    rmse = sqrt(err / sw) if sw else 0.0
    return intercept, slope, rmse


def forecast(history: List[Dict], forecast_days: int = MAX_FORECAST_DAYS):
    """Forecast up to forecast_days after the latest accepted observation."""
    clean = []
    for r in history:
        d = _date(r.get("acquisition_date"))
        y = _f(r.get("observed_ndvi"))
        q = _f(r.get("quality_score"))
        if d is not None and y is not None and q is not None and q >= MIN_QUALITY:
            clean.append((d, y, max(0.05, min(1.0, q))))
    clean.sort()
    if len(clean) < MIN_HISTORY:
        return []

    latest_d, latest_y, _ = clean[-1]
    span = (latest_d - clean[0][0]).days
    if span <= 0 or span > 180:
        return []
    if (latest_d - clean[-2][0]).days > MAX_GAP_DAYS:
        return []

    origin = clean[0][0]
    points = [((d - origin).days, y, q) for d, y, q in clean[-MAX_HISTORY:]]
    fit = _weighted_linear_regression(points)
    if fit is None:
        return []
    intercept, slope, rmse = fit

    # Bound the trend to a conservative daily change. This prevents a sparse,
    # noisy trajectory from producing biologically implausible jumps.
    slope = max(-0.05, min(0.05, slope))
    baseline_uncertainty = max(0.04, rmse * 2.0)
    recent_values = [y for _, y, _ in clean[-4:]]
    recent_spread = max(recent_values) - min(recent_values) if len(recent_values) > 1 else 0
    baseline_uncertainty = min(0.30, baseline_uncertainty + recent_spread * 0.15)

    out = []
    for i in range(1, min(forecast_days, MAX_FORECAST_DAYS) + 1):
        d = latest_d + timedelta(days=i)
        x = (d - origin).days
        predicted = intercept + slope * x
        # Pull the regression forecast slightly toward the latest observation;
        # this is a stability guard, not an agronomic rule.
        predicted = 0.7 * predicted + 0.3 * latest_y
        predicted = max(MIN_PREDICTION, min(MAX_PREDICTION, predicted))
        uncertainty = min(0.35, baseline_uncertainty + 0.02 * i)
        low = max(MIN_PREDICTION, predicted - uncertainty)
        high = min(MAX_PREDICTION, predicted + uncertainty)
        out.append({
            "acquisition_date": d.isoformat(),
            "estimated_ndvi": round(predicted, 6),
            "estimated_ndvi_low": round(low, 6),
            "estimated_ndvi_high": round(high, 6),
            "forecast_horizon_days": i,
            "slope_per_day": round(slope, 6),
            "training_observation_count": len(clean[-MAX_HISTORY:]),
            "rmse": round(rmse, 6),
            "latest_observed_date": latest_d.isoformat(),
            "latest_observed_ndvi": latest_y,
        })
    return out


def load_histories(tenant_id: Optional[str] = None):
    q = (supabase.table("ndvi_data")
         .select("tenant_id,land_id,scene_id,acquisition_date,ndvi_value,quality_score,observation_source,observation_type")
         .eq("observation_source", "sentinel-2")
         .eq("observation_type", "observed")
         .not_.is_("ndvi_value", "null")
         .gte("quality_score", MIN_QUALITY)
         .order("acquisition_date", desc=False))
    if tenant_id:
        q = q.eq("tenant_id", tenant_id)
    rows = with_retry(lambda: q.execute().data or [], what="read temporal NDVI history", attempts=3)
    groups: Dict[Tuple[str, str], List[Dict]] = {}
    for r in rows:
        groups.setdefault((r["tenant_id"], r["land_id"]), []).append({
            "scene_id": r.get("scene_id"),
            "acquisition_date": r.get("acquisition_date"),
            "observed_ndvi": r.get("ndvi_value"),
            "quality_score": r.get("quality_score"),
        })
    return groups


def write_predictions(tenant_id: Optional[str] = None) -> dict:
    groups = load_histories(tenant_id)
    written = 0
    eligible = 0
    skipped = 0
    now = datetime.now(timezone.utc)

    for (tid, land_id), history in groups.items():
        preds = forecast(history)
        if not preds:
            skipped += 1
            continue
        eligible += 1
        latest_scene = sorted(history, key=lambda r: str(r.get("acquisition_date")))[-1].get("scene_id")
        latest = sorted(history, key=lambda r: str(r.get("acquisition_date")))[-1]
        batch = []
        for p in preds:
            scene_id = f"PREDICTED:{land_id}:{p['acquisition_date']}:{MODEL_VERSION}"
            batch.append({
                "tenant_id": tid,
                "land_id": land_id,
                "scene_id": scene_id,
                "acquisition_date": p["acquisition_date"],
                "acquisition_time": None,
                "observed_ndvi": None,
                "estimated_ndvi": p["estimated_ndvi"],
                "estimated_ndvi_low": p["estimated_ndvi_low"],
                "estimated_ndvi_high": p["estimated_ndvi_high"],
                "estimation_method": "weighted_temporal_linear_trend",
                "model_version": MODEL_VERSION,
                "feature_version": FEATURE_VERSION,
                "context_ndvi_mean": None,
                "context_ndvi_median": None,
                "context_ndvi_p10": None,
                "context_ndvi_p90": None,
                "context_ndvi_std": None,
                "context_ndvi_mad": None,
                "parcel_context_delta": None,
                "parcel_context_robust_z": None,
                "context_effective_pixel_count": None,
                "context_observed_fraction": None,
                "context_purity": None,
                "context_cloud_fraction": None,
                "context_shadow_fraction": None,
                "context_water_fraction": None,
                "spatial_anomaly_json": {"source": "temporal_only"},
                "uncertainty_json": {
                    "interval_type": "conservative_temporal_forecast",
                    "rmse": p["rmse"],
                    "horizon_days": p["forecast_horizon_days"],
                    "training_observation_count": p["training_observation_count"],
                },
                "evidence_json": {
                    "latest_observed_scene_id": latest_scene,
                    "latest_observed_date": latest.get("acquisition_date"),
                    "latest_observed_ndvi": latest.get("observed_ndvi"),
                    "training_source": "ndvi_data",
                    "independent_validation": False,
                },
                "provenance_json": {
                    "source": "ndvi_data",
                    "derivation": "temporal_forecast",
                    "model_inference": True,
                    "generated_at": now.isoformat(),
                    "model_version": MODEL_VERSION,
                },
                "intelligence_status": "estimated_unvalidated",
                "validation_status": "not_validated",
                "validation_dataset_version": None,
                "observed_or_predicted": "predicted",
            })
        with_retry(lambda b=batch: supabase.table("ndvi_intelligence").upsert(
            b, on_conflict="tenant_id,land_id,scene_id").execute(),
            what=f"persist temporal predictions for {land_id}", attempts=3)
        written += len(batch)

    result = {
        "tenant_id": tenant_id,
        "lands_with_history": len(groups),
        "eligible_lands": eligible,
        "lands_skipped": skipped,
        "predictions_written": written,
        "model_version": MODEL_VERSION,
        "validation_status": "unvalidated",
    }
    log_step("INTELLIGENCE_FORECAST", "completed", tenant_id, now, metadata=result)
    return result


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--tenant", default=None)
    args = ap.parse_args()
    print(write_predictions(args.tenant))
