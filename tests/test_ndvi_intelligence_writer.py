from ndvi_intelligence_writer import build_observed_intelligence


def test_observed_intelligence_never_creates_prediction():
    row = {
        "tenant_id": "t",
        "land_id": "l",
        "scene_id": "s",
        "acquisition_date": "2026-09-16",
        "acquisition_time": "2026-09-16T05:00:00+00:00",
        "ndvi_value": 0.61,
        "quality_score": 0.82,
        "confidence_level": "high",
        "evidence_confidence": "high",
        "measurement_status": "observed",
        "spatial_stat_method": "fractional_coverage_v3",
        "effective_pixel_count": 9.2,
        "coverage_weighted_purity": 0.91,
        "boundary_contamination_fraction": 0.04,
        "observation_source": "sentinel-2",
        "observation_type": "observed",
        "metadata": {
            "pipeline_version": "v3.1",
            "parcel_context": {
                "context_ndvi_mean": 0.58,
                "context_ndvi_median": 0.57,
                "context_ndvi_p10": 0.49,
                "context_ndvi_p90": 0.66,
                "context_ndvi_std": 0.05,
                "context_ndvi_mad": 0.03,
                "parcel_context_delta": 0.04,
                "parcel_context_robust_z": 0.9,
                "context_effective_pixel_count": 31,
                "context_observed_fraction_of_target": 0.77,
                "context_purity": 0.88,
                "context_cloud_fraction": 0.05,
                "context_shadow_fraction": 0.02,
                "context_water_fraction": 0.01,
            },
            "evidence": {},
            "quality_breakdown": {},
        },
    }
    out = build_observed_intelligence(row)
    assert out["observed_ndvi"] == 0.61
    assert out["estimated_ndvi"] is None
    assert out["model_version"] is None
    assert out["observed_or_predicted"] == "observed"
    assert out["intelligence_status"] == "observed_context"
    assert out["provenance_json"]["model_inference"] is False


def test_missing_context_is_not_fabricated():
    row = {
        "tenant_id": "t", "land_id": "l", "scene_id": "s",
        "acquisition_date": "2026-09-16", "ndvi_value": 0.5,
        "metadata": {},
    }
    out = build_observed_intelligence(row)
    assert out["context_ndvi_mean"] is None
    assert out["evidence_json"]["context_present"] is False
