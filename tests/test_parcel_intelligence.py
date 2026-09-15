import numpy as np
from shapely.geometry import Polygon

from parcel_intelligence import adaptive_context_geometry, constrained_unmix, summarize_context


def test_context_reaches_requested_total_area():
    # ~1000 m2 square parcel, request ~4000 m2 total context.
    parcel = Polygon([(75.0, 20.0), (75.0, 20.0003), (75.0003, 20.0003), (75.0003, 20.0)])
    result = adaptive_context_geometry(parcel, target_total_area_m2=4000.0)
    assert result.achieved_total_area_m2 >= 3999.0
    assert result.context_area_m2 > 0
    assert result.geometry_wgs84.contains(parcel)


def test_context_statistics_are_robust_to_outlier():
    values = np.array([0.60, 0.61, 0.62, 0.61, 0.59, 0.95])
    weights = np.ones(values.shape)
    result = summarize_context(values, weights, parcel_ndvi=0.50)
    assert result.median_ndvi is not None
    assert result.mad_ndvi is not None
    assert result.parcel_minus_context < 0


def test_unmixing_reconstructs_known_two_endmember_mixture():
    crop = np.array([0.20, 0.70, 0.60])
    soil = np.array([0.30, 0.40, 0.35])
    observed = 0.75 * crop + 0.25 * soil
    result = constrained_unmix(observed, [crop, soil])
    fractions = np.array(result["fractions"])
    assert np.isclose(fractions.sum(), 1.0)
    assert fractions[0] > fractions[1]
    assert result["residual_rmse"] < 0.05
