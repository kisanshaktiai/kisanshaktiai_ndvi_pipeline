import numpy as np
from datetime import date

from production_intelligence import (
    ndre_b8a_b05,
    uncertainty_vector,
    cohort_key,
    choose_context_population,
    water_stress_evidence,
    spatial_zone_gate,
)


def test_stac_calibration_semantics_are_scale_then_offset():
    raw = np.array([1000.0, 2000.0, 6000.0], dtype=np.float32)
    scale = 0.0001
    offset = -0.1
    physical = raw * scale + offset
    np.testing.assert_allclose(physical, [0.0, 0.1, 0.5], atol=1e-7)


def test_canonical_ndre_uses_b8a_not_b08():
    b8a = np.array([0.60], dtype=np.float32)
    b05 = np.array([0.30], dtype=np.float32)
    ndre = ndre_b8a_b05(b8a, b05)
    assert np.isclose(float(ndre[0]), 1.0 / 3.0)


def test_uncertainty_gate_is_conservative_minimum():
    u = uncertainty_vector(
        measurement_quality=0.95,
        spatial_support=0.80,
        temporal_support=0.40,
        agreement=0.90,
        model_confidence=None,
    )
    assert u["minimum_gate"] == 0.40
    assert u["model_confidence"] is None


def test_cohort_key_is_crop_week_and_block_and_unknown_date_is_unavailable():
    key = cohort_key(crop_code="cotton", sowing_or_transplant_date=date(2026, 7, 1), block_id="B01")
    assert key.startswith("cotton:") and key.endswith(":B01")
    assert cohort_key(crop_code="cotton", sowing_or_transplant_date=None, block_id="B01") is None


def test_context_population_prefers_cohort_and_only_then_ring():
    assert choose_context_population(cohort_size=8, configured_min_cohort=8, ring_available=True) == "stage_cohort"
    assert choose_context_population(cohort_size=3, configured_min_cohort=8, ring_available=True) == "spatial_ring_fallback"
    assert choose_context_population(cohort_size=3, configured_min_cohort=8, ring_available=False) == "none"


def test_water_stress_requires_all_core_evidence():
    weak = water_stress_evidence(ndmi_decline=True, root_zone_depletion=True, rain_deficit=True, water_sensitive_stage=False)
    strong = water_stress_evidence(ndmi_decline=True, root_zone_depletion=True, rain_deficit=True, water_sensitive_stage=True)
    assert weak["status"] == "insufficient_evidence"
    assert strong["status"] == "corroborated"


def test_spatial_zone_gate_is_closed_without_independent_validation():
    assert not spatial_zone_gate(interior_valid_cells=4, minimum_cells=4, quadrant_kappa=0.5, minimum_kappa=0.5, confirmed_cases=19, minimum_cases=20)
    assert spatial_zone_gate(interior_valid_cells=4, minimum_cells=4, quadrant_kappa=0.5, minimum_kappa=0.5, confirmed_cases=20, minimum_cases=20)
