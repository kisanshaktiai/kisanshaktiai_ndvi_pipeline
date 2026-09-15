"""Independent validation for native vs reconstructed parcel NDVI.

A reconstructed estimate can only be declared better when both native and
estimated predictions are evaluated against an independent reference (for
example UAV/airborne calibrated reflectance or validated field sampling), not
against each other. Splits are grouped by land_id to prevent spatial leakage.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional, Sequence
import math
import numpy as np


@dataclass(frozen=True)
class ValidationMetrics:
    n: int
    mae: Optional[float]
    rmse: Optional[float]
    bias: Optional[float]
    r2: Optional[float]
    within_005: Optional[float]
    within_010: Optional[float]


def _finite_triplets(rows: Iterable[dict], native_key: str, estimate_key: str, reference_key: str):
    out = []
    for r in rows:
        try:
            a, b, y = float(r[native_key]), float(r[estimate_key]), float(r[reference_key])
        except (KeyError, TypeError, ValueError):
            continue
        if all(math.isfinite(v) for v in (a, b, y)):
            out.append((a, b, y, r.get("land_id"), r.get("date") or r.get("acquisition_date")))
    return out


def _metrics(pred: Sequence[float], ref: Sequence[float]) -> ValidationMetrics:
    p = np.asarray(pred, dtype=float)
    y = np.asarray(ref, dtype=float)
    n = int(len(p))
    if n == 0:
        return ValidationMetrics(0, None, None, None, None, None, None)
    e = p - y
    mae = float(np.mean(np.abs(e)))
    rmse = float(np.sqrt(np.mean(e * e)))
    bias = float(np.mean(e))
    denom = float(np.sum((y - np.mean(y)) ** 2))
    r2 = float(1.0 - np.sum(e * e) / denom) if denom > 1e-12 else None
    return ValidationMetrics(
        n=n, mae=mae, rmse=rmse, bias=bias, r2=r2,
        within_005=float(np.mean(np.abs(e) <= 0.05)),
        within_010=float(np.mean(np.abs(e) <= 0.10)),
    )


def paired_validation(
    rows: Iterable[dict],
    native_key: str = "native_ndvi",
    estimate_key: str = "estimated_ndvi",
    reference_key: str = "reference_ndvi",
    min_pairs: int = 30,
) -> dict:
    """Compare both predictions against independent reference measurements."""
    pairs = _finite_triplets(rows, native_key, estimate_key, reference_key)
    native = _metrics([x[0] for x in pairs], [x[2] for x in pairs])
    estimate = _metrics([x[1] for x in pairs], [x[2] for x in pairs])
    if len(pairs) < min_pairs:
        status = "insufficient_reference_pairs"
    else:
        status = "evaluated"

    deltas = np.asarray(
        [abs(x[1] - x[2]) - abs(x[0] - x[2]) for x in pairs], dtype=float
    )
    improvement = float(-np.mean(deltas)) if len(deltas) else None
    improved_fraction = float(np.mean(deltas < 0)) if len(deltas) else None

    return {
        "status": status,
        "n_pairs": len(pairs),
        "minimum_pairs": min_pairs,
        "native": native.__dict__,
        "estimated": estimate.__dict__,
        "paired_absolute_error_improvement": improvement,
        "fraction_pairs_estimate_better": improved_fraction,
        "independent_reference_required": True,
        "no_claim_of_improvement_when_insufficient": True,
    }


def grouped_split(
    rows: Sequence[dict],
    test_fraction: float = 0.20,
    seed: int = 42,
) -> tuple[list[dict], list[dict]]:
    """Split by land_id, never by individual pixels/dates from the same land."""
    groups = sorted({r.get("land_id") for r in rows if r.get("land_id") is not None})
    if not groups:
        return list(rows), []
    rng = np.random.default_rng(seed)
    rng.shuffle(groups)
    n_test = max(1, int(round(len(groups) * test_fraction)))
    test_groups = set(groups[:n_test])
    train = [r for r in rows if r.get("land_id") not in test_groups]
    test = [r for r in rows if r.get("land_id") in test_groups]
    return train, test


def validate_dataset(
    rows: Sequence[dict],
    reference_source: str,
    min_pairs: int = 30,
) -> dict:
    """Validation entrypoint with hard provenance/independence guardrails."""
    if not reference_source or reference_source in {"sentinel-2", "ndvi_data", "native"}:
        raise ValueError("reference_source must be an independent higher-resolution or field reference")
    result = paired_validation(rows, min_pairs=min_pairs)
    result["reference_source"] = reference_source
    result["comparison"] = "native_observed_vs_estimated_against_independent_reference"
    return result
