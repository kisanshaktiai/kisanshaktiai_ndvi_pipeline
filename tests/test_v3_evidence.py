"""
Synthetic regression tests for the NDVI measurement path.
No network and no DB are required.
"""
import os, sys, types, datetime as dt
import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin
from shapely.geometry import box, mapping
from shapely.ops import transform
from pyproj import Transformer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("SUPABASE_URL", "http://localhost")
os.environ.setdefault("SUPABASE_KEY", "test")

import raster_utils as ru          # noqa: E402
from shapely.geometry import box as _shp_box  # noqa: E402
import indices, quality, processor, sar_vegetation  # noqa: E402

CRS = "EPSG:32643"
T10 = from_origin(400000, 1900000, 10, 10)
T20 = from_origin(400000, 1900000, 20, 20)
INV = Transformer.from_crs(CRS, "EPSG:4326", always_xy=True).transform


def _write(path, arr, tr, dtype, nodata):
    with rasterio.open(path, "w", driver="GTiff", height=arr.shape[0], width=arr.shape[1],
                       count=1, dtype=dtype, crs=CRS, transform=tr, nodata=nodata) as d:
        d.write(arr, 1)


def _minimal_item(b4=3000, b8=7000, *, rb_scale=None, rb_offset=None, baseline="05.12"):
    it = types.SimpleNamespace()
    it.id = "S2_TEST"
    it.datetime = dt.datetime(2026, 8, 21, 5, 26, 41, tzinfo=dt.timezone.utc)
    it.properties = {"s2:processing_baseline": baseline}
    bands = {}
    for key, value in (("B04", b4), ("B08", b8)):
        extra = {}
        if rb_scale is not None or rb_offset is not None:
            extra["raster:bands"] = [{"scale": rb_scale, "offset": rb_offset}]
        bands[key] = types.SimpleNamespace(extra_fields=extra)
    it.assets = bands
    return it


def test_stac_scale_offset_is_scale_then_physical_offset():
    item = _minimal_item(rb_scale=0.0001, rb_offset=-0.1)
    b4 = np.array([3000], dtype=np.uint16)
    b8 = np.array([7000], dtype=np.uint16)
    r4 = ru.to_reflectance(b4, item, "B04")
    r8 = ru.to_reflectance(b8, item, "B08")
    assert np.allclose(r4, [0.20])
    assert np.allclose(r8, [0.60])
    ndvi = (r8 - r4) / (r8 + r4)
    assert np.allclose(ndvi, [0.50])


def test_baseline_fallback_applies_dn_offset_before_quantification():
    item = _minimal_item(b4=3000, b8=7000, baseline="05.12")
    r4 = ru.to_reflectance(np.array([3000], dtype=np.uint16), item, "B04")
    r8 = ru.to_reflectance(np.array([7000], dtype=np.uint16), item, "B08")
    assert np.allclose(r4, [0.20])
    assert np.allclose(r8, [0.60])


def test_zero_and_negative_reflectance_do_not_become_ndvi_zero():
    item = _minimal_item(rb_scale=0.0001, rb_offset=-0.1)
    out = ru.to_reflectance(np.array([1000, 900], dtype=np.uint16), item, "B04")
    assert np.isnan(out[0]) and np.isnan(out[1])


@pytest.fixture
def scene(tmp_path):
    rng = np.random.default_rng(7)
    for k, base in (("B02", 1300), ("B03", 1600), ("B04", 1800), ("B08", 5800)):
        _write(tmp_path / f"{k}.tif", (base + rng.integers(-50, 50, (40, 40))).astype("uint16"), T10, "uint16", 0)
    for k, base in (("B05", 2600), ("B11", 2200)):
        _write(tmp_path / f"{k}.tif", (base + rng.integers(-50, 50, (20, 20))).astype("uint16"), T20, "uint16", 0)
    _write(tmp_path / "SCL.tif", np.full((20, 20), 4, "uint8"), T20, "uint8", 0)

    def item(scene_id="S2C_TEST_T43QCU", tile="43QCU"):
        it = types.SimpleNamespace()
        it.id = scene_id
        it.datetime = dt.datetime(2026, 8, 21, 5, 26, 41, tzinfo=dt.timezone.utc)
        it.properties = {"s2:processing_baseline": "05.12", "eo:cloud_cover": 40.0,
                         "platform": "Sentinel-2C", "s2:mgrs_tile": tile, "sat:relative_orbit": 105}
        it.geometry = mapping(transform(INV, box(390000, 1890000, 410000, 1910000)))
        it.assets = {k: types.SimpleNamespace(href=str(tmp_path / f"{k}.tif"), extra_fields={})
                     for k in ("B02", "B03", "B04", "B05", "B08", "B11", "SCL")}
        return it
    return tmp_path, item


def _poly(x0, y0, x1, y1):
    return transform(INV, box(x0, y0, x1, y1))


def test_footprint_excludes_out_of_polygon_cells(scene):
    tmp, item = scene
    poly = _poly(400110, 1899690, 400150, 1899720)
    b4, tr, crs, fp = ru.read_band(item(), "B04", poly)
    ref = (b4.shape, tr, crs, fp)
    b8, *_ = ru.read_band(item(), "B08", poly, reference=ref)
    scl, *_ = ru.read_band(item(), "SCL", poly, reference=ref, categorical=True)
    m = ru.scl_masks(scl, coverage=fp)
    assert abs(m["epc_total"] - 12.0) < 0.05
    ndvi = indices.compute_indices({"B08": b8, "B04": b4})["NDVI"][m["crop"]]
    assert ndvi.min() > 0.6
    assert int((scl != 0).sum()) > 12


def test_zero_reflectance_pixel_is_nan_not_zero():
    out = indices.compute_indices({"B08": np.array([0.5, 0.0]), "B04": np.array([0.1, 0.0])})
    assert abs(out["NDVI"][0] - 2 / 3) < 1e-6 and np.isnan(out["NDVI"][1])


def test_cloud_edge_dilation_reaches_field(scene):
    tmp, item = scene
    poly = _poly(400110, 1899690, 400150, 1899720)
    scl = np.full((20, 20), 4, "uint8"); scl[13, 6] = 9
    _write(tmp / "SCL.tif", scl, T20, "uint8", 0)
    b4, tr, crs, fp = ru.read_band(item(), "B04", poly)
    s, *_ = ru.read_band(item(), "SCL", poly, reference=(b4.shape, tr, crs, fp), categorical=True)
    m = ru.scl_masks(s, coverage=fp)
    assert m["cloud_fraction"] > 0 and m["n_crop_pixels"] < 12


def test_process_land_dedupes_tile_overlap_and_gates_pixels(scene):
    tmp, item = scene
    poly = _poly(400100, 1899680, 400200, 1899750)
    land = {"id": "L1", "tenant_id": "T1", "area_acres": 1.73, "boundary_geom": mapping(poly)}
    rows, rep = processor.process_land(land, scenes=[item("A", "43QCU"), item("B", "43QDU")],
                                       history=[{"acquisition_date": "2026-08-16", "ndvi_value": 0.2, "scene_id": "old"}])
    assert len(rows) == 1 and rep["deduped"] == 1
    r = rows[0]
    ev = r["metadata"]["evidence"]
    assert ev["erosion_applied_m"] == -10.0
    assert ev["measured_area_m2"] < r["field_area_m2"]
    assert abs(ev["effective_pixel_count_total"] * 100 - ev["measured_area_m2"]) / ev["measured_area_m2"] < 0.02
    assert ev["coverage_area_error"] < 0.02
    assert r["ndvi_spatial_min"] > 0.4 and r["source_scene_count"] == 1
    assert r["metadata"]["temporal_outlier"] is True
    assert r["confidence_score"] <= float(np.float32(r["quality_score"])) + 1e-9


def test_quality_confidence_float4_safe():
    m = {"n_field_pixels": 28, "n_crop_pixels": 20, "cloud_fraction": 0.0, "shadow_fraction": 0.0,
         "water_fraction": 0, "snow_fraction": 0, "saturated_fraction": 0, "unaccounted_fraction": 0}
    qa = quality.assess(m, buffer_applied=False, area_acres=0.33, geometry_confidence="high")
    assert qa.confidence_score <= float(np.float32(qa.quality_score)) + 1e-9


def test_rvi_dual_pol_range():
    r = sar_vegetation.rvi_from_gamma0(np.full(20, 0.10), np.full(20, 0.05))
    assert abs(r["rvi_mean"] - 4 * 0.05 / 0.15) < 1e-3 and r["rvi_mean"] > 1.0


def test_coverage_is_area_true():
    tr = from_origin(400000, 1900000, 10, 10)
    poly = _shp_box(400013, 1899947, 400074, 1899988)
    cov, method = ru.coverage_fractions(poly, tr, (10, 10))
    assert method == "exact_shapely"
    assert abs(cov.sum() * 100.0 - poly.area) / poly.area < 1e-4


def test_ten_guntha_field_is_measured_not_eroded(scene):
    tmp, item = scene
    poly = _poly(400010, 1899950, 400042, 1899982)
    geom_m, buffered, raw_area, meas_area = ru.measurement_field(poly)
    assert buffered is False and abs(meas_area - raw_area) < 1e-6
    land = {"id": "TEN_GUNTHA", "tenant_id": "T1", "area_acres": 0.25,
            "boundary_geom": mapping(poly)}
    rows, rep = processor.process_land(land, scenes=[item()], history=[])
    assert len(rows) == 1, rep["optical_rejects"]
    ev = rows[0]["metadata"]["evidence"]
    assert abs(ev["effective_pixel_count_total"] * 100 - raw_area) / raw_area < 0.02
    assert ev["spatial_stat_method"] == "fractional_coverage_v3"
    assert ev["measurement_status"] in ("OBSERVED_STRONG", "OBSERVED_LIMITED")
    assert ev["ndvi_spatial_se"] is not None
    assert rows[0]["ndvi_histogram"]["weighting"] == "coverage_area_effective_pixels"


def test_boundary_cells_cannot_outvote_interior_area():
    v = np.array([0.75, 0.75, 0.10, 0.10, 0.10, 0.10, 0.10, 0.10])
    w = np.array([1.00, 1.00, 0.25, 0.25, 0.25, 0.25, 0.25, 0.25])
    st = indices.weighted_index_statistics(v, w)
    assert abs(st["epc"] - 3.5) < 1e-9 and st["n_cells"] == 8
    assert abs(st["purity"] - 0.4375) < 1e-6
    assert st["mean"] > 0.4
    from quality import evidence_tier
    assert evidence_tier(2.0)[0] == "INSUFFICIENT_SPATIAL_SUPPORT"
    assert evidence_tier(8.5)[1] == "high"


def test_purity_demotes_tier_but_never_into_a_reject():
    from quality import evidence_tier
    assert evidence_tier(8.34, 0.48) == ("OBSERVED_LIMITED", "medium")
    assert evidence_tier(8.34, 0.97) == ("OBSERVED_STRONG", "high")
    assert evidence_tier(3.5, 0.50) == ("OBSERVED_WEAK", "low")
    assert evidence_tier(2.0, 0.99) == ("INSUFFICIENT_SPATIAL_SUPPORT", "insufficient")
    assert evidence_tier(9.0, None) == ("OBSERVED_STRONG", "high")


def test_run_version_matches_row_version():
    import re
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "main.py")).read()
    assert "PIPELINE_VERSION" in src
    assert not re.search(r'NDVI v\d+\.\d+ (start|finished)', src)
    assert '"pipeline_version": "v2' not in src
