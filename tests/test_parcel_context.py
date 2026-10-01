import types
import datetime as dt

import numpy as np
from shapely.geometry import box

import parcel_context as pc


def test_neighbour_ring_uses_clean_pixels_outside_the_bund_gap(monkeypatch):
    """Approved ring rule (2026-09-23): neighbours are WHOLE clean pixels the
    field polygon does not touch, at least CONTEXT_RING_GAP_M from its
    boundary; the ring grows until CONTEXT_CLEAN_PX_REAL pixels. The field is
    never its own neighbour, and cells touching it (bund / shared edge) are
    excluded."""
    from rasterio.transform import from_origin
    from pyproj import Transformer
    from shapely.ops import transform as shp_transform
    from config import CONTEXT_RING_GAP_M, CONTEXT_CLEAN_PX_REAL
    item = types.SimpleNamespace(id="S2_CONTEXT_TEST",
                                 datetime=dt.datetime(2026, 9, 12, tzinfo=dt.timezone.utc))
    crs = "EPSG:32643"
    tr = from_origin(400000, 1900000, 10, 10)                   # 21 x 21 cells of 10 m
    shape = (21, 21)
    parcel_utm = box(400100, 1899890, 400110, 1899900)         # exactly the centre cell (10, 10)
    parcel = shp_transform(Transformer.from_crs(crs, "EPSG:4326", always_xy=True).transform, parcel_utm)
    ones = np.ones(shape, dtype="float32")
    b04 = np.full(shape, 0.2, dtype="float32"); b08 = np.full(shape, 0.6, dtype="float32")
    b04[10, 10] = 0.9                                           # the field itself is very different

    def fake_read_band(_item, band, _geom, reference=None, categorical=False):
        return {"B04": b04, "B08": b08, "SCL": np.full(shape, 4, dtype="int16")}[band], tr, crs, ones
    monkeypatch.setattr(pc, "read_band", fake_read_band)

    r = pc.extract_parcel_context(item, parcel)
    assert r["status"] == "observed_context" and r["context_tier"] == "real"
    assert r["context_clean_pixels"] >= CONTEXT_CLEAN_PX_REAL
    assert r["context_buffer_m"] > CONTEXT_RING_GAP_M
    assert r["context_ndvi_median"] > 0.4                      # field pixel (NDVI < 0) never counted
    assert r["provenance"]["observed_or_estimated"] == "observed"
    assert r["provenance"]["context_is_not_parcel_measurement"] is True
    assert r["context_weight_method"] == "clean_neighbour_ring_v3"


def test_ring_with_too_few_clean_pixels_gives_no_comparison(monkeypatch):
    from rasterio.transform import from_origin
    from pyproj import Transformer
    from shapely.ops import transform as shp_transform
    item = types.SimpleNamespace(id="S2_CTX_CLOUDY", datetime=dt.datetime(2026, 9, 12, tzinfo=dt.timezone.utc))
    crs = "EPSG:32643"; tr = from_origin(400000, 1900000, 10, 10); shape = (21, 21)
    parcel = shp_transform(Transformer.from_crs(crs, "EPSG:4326", always_xy=True).transform,
                           box(400100, 1899890, 400110, 1899900))
    scl = np.full(shape, 9, dtype="int16")                      # the whole neighbourhood is cloud
    def fake_read_band(_item, band, _geom, reference=None, categorical=False):
        return {"B04": np.full(shape, 0.2, "float32"), "B08": np.full(shape, 0.6, "float32"),
                "SCL": scl}[band], tr, crs, np.ones(shape, "float32")
    monkeypatch.setattr(pc, "read_band", fake_read_band)
    r = pc.extract_parcel_context(item, parcel)
    assert r["status"] == "insufficient_context" and "context_ndvi_median" not in r


def test_parcel_delta_is_not_a_diagnosis():
    context = {"context_ndvi_median": 0.60, "context_ndvi_mad": 0.05, "context_tier": "real",
               "context_clean_pixels": 25}
    result = pc.add_parcel_delta(context, 0.50)
    assert np.isclose(result["parcel_context_delta"], -0.10)
    assert result["parcel_context_robust_z"] < 0
    assert "diagnosis" not in result


def test_z_includes_measurement_uncertainty_and_indicative_ring_has_no_verdict():
    """Old z = delta / (1.4826*MAD) exploded when a small uniform ring had MAD ~ 0.
    Now both sides' measurement error enter the denominator, and an indicative
    ring publishes the difference but never a verdict."""
    tight = {"context_ndvi_median": 0.60, "context_ndvi_mad": 0.001, "context_tier": "real",
             "context_clean_pixels": 25}
    old_style = -0.05 / (1.4826 * 0.001)                        # about -34: a false alarm
    new = pc.add_parcel_delta(tight, 0.55, parcel_se=0.02)["parcel_context_robust_z"]
    assert abs(new) < 3 and abs(old_style) > 30
    ind = pc.add_parcel_delta({**tight, "context_tier": "indicative"}, 0.55, parcel_se=0.02)
    assert ind["parcel_context_delta"] is not None and ind["parcel_context_robust_z"] is None
