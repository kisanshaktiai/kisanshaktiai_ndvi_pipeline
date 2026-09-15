"""Statistical evidence layer for independent parcel-NDVI validation."""

from __future__ import annotations

import math
from collections import defaultdict
import numpy as np


def _finite_pairs(rows, native_key, estimated_key, reference_key):
    pairs = []
    for r in rows:
        try:
            a = float(r[native_key]); b = float(r[estimated_key]); y = float(r[reference_key])
        except (KeyError, TypeError, ValueError):
            continue
        if all(math.isfinite(v) for v in (a, b, y)):
            land_id = r.get("land_id")
            if land_id is not None:
                pairs.append((land_id, a, b, y))
    return pairs


def paired_mae_bootstrap(rows, native_key="native_ndvi", estimated_key="estimated_ndvi",
                         reference_key="reference_ndvi", min_pairs=30,
                         min_lands=10, resamples=2000, seed=42):
    """Return a land-clustered paired bootstrap CI for native MAE minus estimated MAE.

    Positive values mean the estimated prediction has lower absolute error.
    Repeated dates/pixels from one parcel are resampled as one cluster so a
    single farm cannot masquerade as many independent farms.
    """
    pairs = _finite_pairs(rows, native_key, estimated_key, reference_key)
    n = len(pairs)
    lands = defaultdict(list)
    for land_id, a, b, y in pairs:
        lands[land_id].append((a, b, y))
    n_lands = len(lands)

    if n < min_pairs or n_lands < min_lands:
        return {
            "status": "insufficient_reference_pairs_or_lands",
            "n_pairs": n,
            "n_lands": n_lands,
            "min_pairs": min_pairs,
            "min_lands": min_lands,
            "supported": False,
            "bootstrap_unit": "land_id",
        }

    land_keys = list(lands)
    native_err = np.asarray([abs(a - y) for _, a, _, y in pairs], dtype=float)
    estimated_err = np.asarray([abs(b - y) for _, _, b, y in pairs], dtype=float)
    observed = float(native_err.mean() - estimated_err.mean())

    rng = np.random.default_rng(seed)
    boot = np.empty(resamples, dtype=float)
    for i in range(resamples):
        sampled = rng.choice(land_keys, size=n_lands, replace=True)
        ne = []
        ee = []
        for land_id in sampled:
            for a, b, y in lands[land_id]:
                ne.append(abs(a - y))
                ee.append(abs(b - y))
        boot[i] = np.mean(ne) - np.mean(ee)
    lo, hi = np.quantile(boot, [0.025, 0.975])

    return {
        "status": "evaluated",
        "n_pairs": n,
        "n_lands": n_lands,
        "min_pairs": min_pairs,
        "min_lands": min_lands,
        "native_mae": float(native_err.mean()),
        "estimated_mae": float(estimated_err.mean()),
        "mae_improvement_native_minus_estimated": observed,
        "ci95_lower": float(lo),
        "ci95_upper": float(hi),
        "fraction_estimated_better": float(np.mean(estimated_err < native_err)),
        "supported": bool(lo > 0),
        "interpretation": "positive improvement means estimated MAE is lower; CI must exclude zero",
        "reference_must_be_independent": True,
        "bootstrap_unit": "land_id",
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
