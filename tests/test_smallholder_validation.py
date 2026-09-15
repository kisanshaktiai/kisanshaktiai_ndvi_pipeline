import unittest
import numpy as np

from parcel_intelligence import adaptive_context_geometry, robust_location
from validation_framework import paired_validation, grouped_split, validate_dataset


class SmallholderValidationTests(unittest.TestCase):
    def test_context_target_is_metric_and_preserves_parcel(self):
        from shapely.geometry import Polygon
        parcel = Polygon([
            (73.0, 18.0), (73.0003, 18.0),
            (73.0003, 18.0003), (73.0, 18.0003),
        ])
        result = adaptive_context_geometry(parcel, target_total_area_m2=4046.8564224)
        self.assertGreaterEqual(result.achieved_total_area_m2, 4046.0)
        self.assertGreater(result.context_area_m2, 0)
        self.assertGreater(result.buffer_m, 0)

    def test_weighted_median_mad(self):
        med, mad = robust_location(np.array([0.2, 0.3, 0.4]), np.array([1.0, 2.0, 1.0]))
        self.assertEqual(med, 0.3)
        # Weighted median has mass 0.5 at 0.3, so the weighted MAD is exactly 0.
        self.assertAlmostEqual(mad, 0.0, places=6)

    def test_validation_requires_independent_reference(self):
        rows = [
            {"land_id": "a", "native_ndvi": 0.40, "estimated_ndvi": 0.50, "reference_ndvi": 0.52},
            {"land_id": "b", "native_ndvi": 0.60, "estimated_ndvi": 0.55, "reference_ndvi": 0.56},
        ]
        result = paired_validation(rows, min_pairs=2)
        self.assertEqual(result["status"], "evaluated")
        self.assertEqual(result["n_pairs"], 2)
        self.assertTrue(result["independent_reference_required"])

        with self.assertRaises(ValueError):
            validate_dataset(rows, reference_source="ndvi_data", min_pairs=2)

    def test_grouped_split_has_no_land_leakage(self):
        rows = [{"land_id": str(i), "x": j} for i in range(10) for j in range(3)]
        train, test = grouped_split(rows, test_fraction=0.2, seed=42)
        train_ids = {r["land_id"] for r in train}
        test_ids = {r["land_id"] for r in test}
        self.assertTrue(train_ids.isdisjoint(test_ids))

    def test_insufficient_reference_does_not_claim_improvement(self):
        rows = [{"land_id": "a", "native_ndvi": 0.4, "estimated_ndvi": 0.5, "reference_ndvi": 0.51}]
        result = paired_validation(rows, min_pairs=30)
        self.assertEqual(result["status"], "insufficient_reference_pairs")
        self.assertTrue(result["no_claim_of_improvement_when_insufficient"])


if __name__ == "__main__":
    unittest.main()
