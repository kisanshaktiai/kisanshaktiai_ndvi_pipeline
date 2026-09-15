import unittest

from validation_evidence import paired_mae_bootstrap, leakage_audit


class ValidationEvidenceTests(unittest.TestCase):
    def test_insufficient_reference_is_not_supported(self):
        rows = [{"native_ndvi": 0.4, "estimated_ndvi": 0.5, "reference_ndvi": 0.52}]
        result = paired_mae_bootstrap(rows, min_pairs=30)
        self.assertFalse(result["supported"])
        self.assertEqual(result["status"], "insufficient_reference_pairs")

    def test_no_land_leakage(self):
        result = leakage_audit([{}], ["a", "b"], ["c"])
        self.assertTrue(result["passed"])
        result = leakage_audit([{}], ["a"], ["a"])
        self.assertFalse(result["passed"])


if __name__ == "__main__":
    unittest.main()
