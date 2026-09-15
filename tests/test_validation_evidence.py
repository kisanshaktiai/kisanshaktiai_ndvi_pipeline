import unittest

from validation_evidence import paired_mae_bootstrap, leakage_audit


class ValidationEvidenceTests(unittest.TestCase):
    def test_insufficient_reference_is_not_supported(self):
        rows = [{"land_id": "a", "native_ndvi": 0.4, "estimated_ndvi": 0.5, "reference_ndvi": 0.52}]
        result = paired_mae_bootstrap(rows, min_pairs=30)
        self.assertFalse(result["supported"])
        self.assertEqual(result["status"], "insufficient_reference_pairs_or_lands")
        self.assertEqual(result["n_lands"], 1)

    def test_clustered_bootstrap_counts_farms_not_rows(self):
        rows = []
        for land_id in ["a", "b", "c", "d", "e", "f", "g", "h", "i", "j"]:
            for _ in range(3):
                rows.append({"land_id": land_id, "native_ndvi": 0.40,
                             "estimated_ndvi": 0.50, "reference_ndvi": 0.52})
        result = paired_mae_bootstrap(rows, min_pairs=30, min_lands=10, resamples=100, seed=7)
        self.assertEqual(result["status"], "evaluated")
        self.assertEqual(result["n_pairs"], 30)
        self.assertEqual(result["n_lands"], 10)
        self.assertEqual(result["bootstrap_unit"], "land_id")

    def test_no_land_leakage(self):
        result = leakage_audit([{}], ["a", "b"], ["c"])
        self.assertTrue(result["passed"])
        result = leakage_audit([{}], ["a"], ["a"])
        self.assertFalse(result["passed"])


if __name__ == "__main__":
    unittest.main()
