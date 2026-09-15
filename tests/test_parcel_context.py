import types
import datetime as dt

import numpy as np
from shapely.geometry import box

import parcel_context as pc


def test_same_scene_context_excludes_parcel_and_preserves_observed_semantics(monkeypatch):
    item = types.SimpleNamespace(
        id="S2_CONTEXT_TEST",
        datetime=dt.datetime(2026, 9, 12, tzinfo=dt.timezone.utc),
    )
    parcel = box(0, 0, 10, 10)
    context = box(-10, -10, 20, 20)
    transform = types.SimpleNamespace(a=10.0, b=0.0, c=-10.0, d=0.0, e=-10.0, f=20.0)
    crs = "EPSG:32643"
    context_cov = np.ones((3, 3), dtype="float32")
    parcel_cov = np.zeros((3, 3), dtype="float32")
    parcel_cov[1, 1] = 1.0

    def fake_read_band(_item, band, _geom, reference=None, categorical=False):
        if band == "B04":
            return np.full((3, 3), 0.2, dtype="float32"), transform, crs, context_cov
        if band == "B08":
            return np.full((3, 3), 0.6, dtype="float32"), transform, crs, context_cov
        if band == "SCL":
            return np.full((3, 3), 4, dtype="int16"), transform, crs, context_cov
        raise AssertionError(band)

    monkeypatch.setattr(pc, "read_band", fake_read_band)
    monkeypatch.setattr(pc, "coverage_fractions", lambda *_: (parcel_cov, "exact_shapely"))
    monkeypatch.setattr(pc, "adaptive_context_geometry", lambda *_args, **_kwargs: types.SimpleNamespace(
        geometry_wgs84=context, achieved_total_area_m2=900.0, buffer_m=10.0
    ))

    result = pc.extract_parcel_context(item, parcel, target_area_m2=900.0)

    assert result["scene_id"] == "S2_CONTEXT_TEST"
    assert result["provenance"]["observed_or_estimated"] == "observed"
    assert result["provenance"]["context_is_not_parcel_measurement"] is True
    assert result["context_valid_crop_area_m2"] == 800.0
    assert result["context_effective_pixel_count"] == 8.0
    assert result["context_weight_method"] == "exact_fractional_context_minus_parcel_v2"


def test_parcel_delta_is_not_a_diagnosis():
    context = {"context_ndvi_median": 0.60, "context_ndvi_mad": 0.05}
    result = pc.add_parcel_delta(context, 0.50)
    assert np.isclose(result["parcel_context_delta"], -0.10)
    assert result["parcel_context_robust_z"] < 0
    assert "diagnosis" not in result
