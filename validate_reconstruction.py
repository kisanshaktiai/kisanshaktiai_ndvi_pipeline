"""CLI for independent parcel-NDVI validation.

Input CSV must contain:
  land_id,date,native_ndvi,estimated_ndvi,reference_ndvi

`reference_ndvi` must come from an independently acquired/validated source,
not from ndvi_data or the Sentinel-2 pipeline under evaluation.
"""

from __future__ import annotations

import argparse
import csv
import json

from validation_framework import grouped_split
from validation_evidence import paired_mae_bootstrap, leakage_audit


def load_rows(path):
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("csv")
    ap.add_argument("--reference-source", required=True)
    ap.add_argument("--min-pairs", type=int, default=30)
    ap.add_argument("--test-fraction", type=float, default=0.20)
    args = ap.parse_args()

    rows = load_rows(args.csv)
    train, test = grouped_split(rows, test_fraction=args.test_fraction)
    leakage = leakage_audit(rows,
                             [r.get("land_id") for r in train],
                             [r.get("land_id") for r in test])

    if not leakage["passed"]:
        raise SystemExit("LEAKAGE_GUARD_FAILED: land_id appears in both train and test")

    result = paired_mae_bootstrap(
        test,
        min_pairs=args.min_pairs,
    )
    result.update({
        "reference_source": args.reference_source,
        "comparison": "native_observed_vs_estimated_against_independent_reference",
        "test_land_count": len({r.get("land_id") for r in test}),
        "train_land_count": len({r.get("land_id") for r in train}),
        "leakage_audit": leakage,
    })
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
