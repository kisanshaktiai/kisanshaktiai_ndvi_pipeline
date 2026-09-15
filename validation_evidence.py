"""Statistical evidence layer for independent parcel-NDVI validation."""

from __future__ import annotations

import math
import numpy as np


def paired_mae_bootstrap(rows, native_key="native_ndvi", estimated_key="estimated_ndvi",
                         reference_key="reference_ndvi", min_pairs=30,
                         resamples=2000, seed=42):
    """Return a paired bootstrap CI for native MAE minus estimated MAE.

    Positive values mean the estimated prediction has lower absolute error.
    No production promotion decision is made here; the caller must also check
    independent provenance, geographic holdout, temporal separation and sample
    coverage.
    """
    pairs = []
    for r in rows:
        try:
            a = float(r[native_key]); b = float(r[estimated_key]); y = float(r[reference_key])
        except (KeyError, TypeError, ValueError):
            continue
        if all(math.isfinite(v) for v in (a, b, y)):
            pairs.append((a, b, y))

    n = len(pairs)
    if n < min_pairs:
        return {
            "status": "insufficient_reference_pairs",
            "n_pairs": n,
            "min_pairs": min_pairs,
            "supported": False,
        }

    native_err = np.abs(np.asarray([p[0] - p[2] for p in pairs]))
    estimated_err = np.abs(np.asarray([p[1] - p[2] for p in pairs]))
    observed = float(native_err.mean() - estimated_err.mean())

    rng = np.random.default_rng(seed)
    idx = rng.integers(0, n, size=(resamples, n))
    boot = native_err[idx].mean(axis=1) - estimated_err[idx].mean(axis=1)
    lo, hi = np.quantile(boot, [0.025, 0.975])

    return {
        "status": "evaluated",
        "n_pairs": n,
        "min_pairs": min_pairs,
        "native_mae": float(native_err.mean()),
        "estimated_mae": float(estimated_err.mean()),
        "mae_improvement_native_minus_estimated": observed,
        "ci95_lower": float(lo),
        "ci95_upper": float(hi),
        "fraction_estimated_better": float(np.mean(estimated_err < native_err)),
        "supported": bool(lo > 0),
        "interpretation": "positive improvement means estimated MAE is lower; CI must exclude zero",
        "reference_must_be_independent": True,
    }


def leakage_audit(rows, train_ids, test_ids):
    """Hard guard against parcel/land leakage between train and test sets."""
    train_ids, test_ids = set(train_ids), set(test_ids)
    overlap = sorted(train_ids.intersection(test_ids))
    return {
        "passed": len(overlap) == 0,
        "overlap_land_ids": overlap,
        "train_land_count": len(train_ids),
        "test_land_count": len(test_ids),
    }
