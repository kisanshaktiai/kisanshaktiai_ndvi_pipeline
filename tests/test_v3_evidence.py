"""
Synthetic regression tests for the v2.2 integrity fixes (no network, no DB).
Run:  SUPABASE_URL=http://x SUPABASE_KEY=x python -m pytest tests -q
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
    poly = _poly(400110, 1899690, 400150, 1899720)          # 4 x 3 cells, straddles 20 m grid
    b4, tr, crs, fp = ru.read_band(item(), "B04", poly)   # fp is now coverage
    ref = (b4.shape, tr, crs, fp)
    b8, *_ = ru.read_band(item(), "B08", poly, reference=ref)
    scl, *_ = ru.read_band(item(), "SCL", poly, reference=ref, categorical=True)
    m = ru.scl_masks(scl, coverage=fp)
    # 12 whole cells inside; all_touched selection adds boundary cells whose
    # coverage weights are what keep the measurement area-true.
    assert abs(m["epc_total"] - 12.0) < 0.05
    ndvi = indices.compute_indices({"B08": b8, "B04": b4})["NDVI"][m["crop"]]
    assert ndvi.min() > 0.6                                  # no 0.0 contamination
    assert int((scl != 0).sum()) > 12                        # the OLD footprint would have over-counted


def test_zero_reflectance_pixel_is_nan_not_zero():
    out = indices.compute_indices({"B08": np.array([0.5, 0.0]), "B04": np.array([0.1, 0.0])})
    assert abs(out["NDVI"][0] - 2 / 3) < 1e-6 and np.isnan(out["NDVI"][1])


def test_cloud_edge_dilation_reaches_field(scene):
    tmp, item = scene
    poly = _poly(400110, 1899690, 400150, 1899720)
    scl = np.full((20, 20), 4, "uint8"); scl[13, 6] = 9      # cloud one native cell above field
    _write(tmp / "SCL.tif", scl, T20, "uint8", 0)
    b4, tr, crs, fp = ru.read_band(item(), "B04", poly)   # fp is now coverage
    s, *_ = ru.read_band(item(), "SCL", poly, reference=(b4.shape, tr, crs, fp), categorical=True)
    m = ru.scl_masks(s, coverage=fp)
    assert m["cloud_fraction"] > 0 and m["n_crop_pixels"] < 12


def test_process_land_dedupes_tile_overlap_and_gates_pixels(scene):
    tmp, item = scene
    poly = _poly(400100, 1899680, 400200, 1899750)           # 7000 m2
    land = {"id": "L1", "tenant_id": "T1", "area_acres": 1.73, "boundary_geom": mapping(poly)}
    rows, rep = processor.process_land(land, scenes=[item("A", "43QCU"), item("B", "43QDU")],
                                       history=[{"acquisition_date": "2026-08-16", "ndvi_value": 0.2, "scene_id": "old"}])
    assert len(rows) == 1 and rep["deduped"] == 1
    r = rows[0]
    ev = r["metadata"]["evidence"]
    # Area identity replaces the old pixel-count bound. It holds against the
    # MEASURED polygon: this 7000 m2 field is above the adaptive-erosion
    # threshold, so the measured area is the eroded one and both are stored.
    assert ev["erosion_applied_m"] == -10.0
    assert ev["measured_area_m2"] < r["field_area_m2"]
    assert abs(ev["effective_pixel_count_total"] * 100 - ev["measured_area_m2"]) / ev["measured_area_m2"] < 0.02
    assert ev["coverage_area_error"] < 0.02
    assert r["ndvi_spatial_min"] > 0.4 and r["source_scene_count"] == 1
    assert r["metadata"]["temporal_outlier"] is True
    assert r["confidence_score"] <= float(np.float32(r["quality_score"])) + 1e-9   # F-2 guard


def test_quality_confidence_float4_safe():
    m = {"n_field_pixels": 28, "n_crop_pixels": 20, "cloud_fraction": 0.0, "shadow_fraction": 0.0,
         "water_fraction": 0, "snow_fraction": 0, "saturated_fraction": 0, "unaccounted_fraction": 0}
    qa = quality.assess(m, buffer_applied=False, area_acres=0.33, geometry_confidence="high")
    assert qa.confidence_score <= float(np.float32(qa.quality_score)) + 1e-9


def test_rvi_dual_pol_range():
    r = sar_vegetation.rvi_from_gamma0(np.full(20, 0.10), np.full(20, 0.05))
    assert abs(r["rvi_mean"] - 4 * 0.05 / 0.15) < 1e-3 and r["rvi_mean"] > 1.0




# ===========================================================================
# v3 SMALLHOLDER EVIDENCE TESTS
# ===========================================================================
def test_coverage_is_area_true():
    """EPC * 100 m2 must equal the polygon area: the identity that replaces
    the v2.2 pixel-count plausibility heuristic."""
    from rasterio.transform import from_origin
    tr = from_origin(400000, 1900000, 10, 10)
    poly = _shp_box(400013, 1899947, 400074, 1899988)      # 61 x 41 m = 2501 m2
    cov, method = ru.coverage_fractions(poly, tr, (10, 10))
    assert method == "exact_shapely"
    assert abs(cov.sum() * 100.0 - poly.area) / poly.area < 1e-4


def test_ten_guntha_field_is_measured_not_eroded(scene):
    """A 10-guntha (~1012 m2) field must be measured on the farmer's own
    polygon - a fixed -10 m erosion would delete ~86 % of it - and must
    yield EPC ~= area/100 with an explicit evidence tier."""
    tmp, item = scene
    poly = _poly(400010, 1899950, 400042, 1899982)          # 32 x 32 m = 1024 m2
    geom_m, buffered, raw_area, meas_area = ru.measurement_field(poly)
    assert buffered is False and abs(meas_area - raw_area) < 1e-6

    land = {"id": "TEN_GUNTHA", "tenant_id": "T1", "area_acres": 0.25,
            "boundary_geom": mapping(poly)}
    rows, rep = processor.process_land(land, scenes=[item()], history=[])
    assert len(rows) == 1, rep["optical_rejects"]
    ev = rows[0]["metadata"]["evidence"]
    assert abs(ev["effective_pixel_count_total"] * 100 - raw_area) / raw_area < 0.02
    assert ev["spatial_stat_method"] == "fractional_coverage_v3"
    # Accuracy rule (agronomic decision 2026-09-23): graded on CLEAN interior
    # pixels, not paper area. This 32 x 32 m field has 9 whole interior cells,
    # below CLEAN_PX_REAL (10), so it is KEPT and labelled indicative - it was
    # "STRONG/LIMITED" only under the old paper-area rule.
    assert ev["measurement_status"] == "OBSERVED_WEAK"
    assert ev["ndvi_spatial_se"] is not None
    assert rows[0]["ndvi_histogram"]["weighting"] == "coverage_area_effective_pixels"


def test_boundary_cells_cannot_outvote_interior_area():
    """Eight cells 25 % inside the field are EPC 2.0, not 8 - and the mean
    must follow the area, not the cell count."""
    import indices
    v = np.array([0.75, 0.75, 0.10, 0.10, 0.10, 0.10, 0.10, 0.10])
    w = np.array([1.00, 1.00, 0.25, 0.25, 0.25, 0.25, 0.25, 0.25])
    st = indices.weighted_index_statistics(v, w)
    assert abs(st["epc"] - 3.5) < 1e-9 and st["n_cells"] == 8
    assert abs(st["purity"] - 0.4375) < 1e-6
    assert st["mean"] > 0.4                       # unweighted mean would be 0.2625
    from quality import evidence_tier
    assert evidence_tier(2.0)[0] == "INSUFFICIENT_SPATIAL_SUPPORT"
    assert evidence_tier(8.5)[1] == "high"


@pytest.mark.skipif(__import__("importlib").util.find_spec("supabase") is None,
                    reason="supabase client not installed in this environment")
def test_unknown_columns_are_filtered_not_fatal():
    """Deploying v3 before the migration must not fail the upsert: unknown
    evidence columns are dropped, metadata.evidence still carries them."""
    import db
    db._KNOWN_COLUMNS = {"land_id", "scene_id", "ndvi_value", "metadata"}
    out = db._filter_to_schema([{"land_id": "L", "scene_id": "S", "ndvi_value": 0.5,
                                 "effective_pixel_count": 9.2,
                                 "metadata": {"evidence": {"effective_pixel_count": 9.2}}}])
    assert "effective_pixel_count" not in out[0]
    assert out[0]["metadata"]["evidence"]["effective_pixel_count"] == 9.2
    db._KNOWN_COLUMNS = None


def test_purity_demotes_tier_but_never_into_a_reject():
    """v3.0.1: low purity drops the evidence tier one band so the decision
    layer cannot treat half-cells as whole pixels - but it must never reach
    'insufficient', which is a hard reject reserved for EPC < MIN_EPC."""
    from quality import evidence_tier
    # live case, land 3307fac1 run 33286187042: EPC 8.34, purity 0.48
    assert evidence_tier(8.34, 0.48) == ("OBSERVED_LIMITED", "medium")
    assert evidence_tier(8.34, 0.97) == ("OBSERVED_STRONG", "high")
    # demotion floors at "low" - storage stays governed by EPC alone
    assert evidence_tier(3.5, 0.50) == ("OBSERVED_WEAK", "low")
    # genuinely unsupported stays unsupported regardless of purity
    assert evidence_tier(2.0, 0.99) == ("INSUFFICIENT_SPATIAL_SUPPORT", "insufficient")
    # no purity available -> EPC-only result, never a guess
    assert evidence_tier(9.0, None) == ("OBSERVED_STRONG", "high")


def test_run_version_matches_row_version():
    """v3.0.1: the version in logs and ndvi_run_summary.notes is the same
    constant stamped into every row, so a run can never report v2.2 while
    writing v3 rows (observed in run 33286187042)."""
    import re
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "main.py")).read()
    assert "PIPELINE_VERSION" in src
    assert not re.search(r'NDVI v\d+\.\d+ (start|finished)', src)
    assert '"pipeline_version": "v2' not in src


# ===========================================================================
# v3.1 FIELD IMAGERY
# ===========================================================================
def test_ndvi_png_is_warped_to_polygon_wgs84_bbox():
    """The app adds the PNG as a MapLibre image source pinned to
    computeBounds(boundary) - the polygon's WGS84 bbox, north-up. If the
    pipeline handed over the native UTM window the heatmap would sit
    crooked and offset over the farmer's field."""
    import raster_io
    from rasterio.transform import from_origin
    from shapely.ops import transform as shp_transform
    from pyproj import Transformer
    from shapely.geometry import box as _b

    tr = from_origin(400000, 1900000, 10, 10)
    ndvi = np.tile(np.linspace(0.15, 0.85, 12), (10, 1)).astype("float32")
    vis = np.ones((10, 12), dtype="float32")
    inv = Transformer.from_crs("EPSG:32643", "EPSG:4326", always_xy=True).transform
    poly = shp_transform(inv, _b(400000, 1899900, 400120, 1900000))

    png, meta = raster_io.render_ndvi_png(ndvi, vis, tr, "EPSG:32643", poly)
    assert meta["crs"] == "EPSG:4326"
    w, s, e, n = poly.bounds
    assert abs(meta["bounds_wgs84"]["west"] - w) < 1e-12
    assert abs(meta["bounds_wgs84"]["north"] - n) < 1e-12
    # aspect must follow GROUND distance (120 x 100 m), not degrees
    assert abs(meta["width"] / meta["height"] - 1.2) < 0.06
    assert meta["resampling"] == "nearest"          # never invent a value
    assert png[:8] == b"\x89PNG\r\n\x1a\n"


def test_masked_cells_are_transparent_not_coloured():
    """Cloud, shadow, non-crop and out-of-polygon cells must be absent from
    the image, not painted a colour the farmer would read as a measurement."""
    import raster_io
    from rasterio.transform import from_origin
    from shapely.ops import transform as shp_transform
    from pyproj import Transformer
    from shapely.geometry import box as _b
    from PIL import Image
    import io as _io

    tr = from_origin(400000, 1900000, 10, 10)
    ndvi = np.full((10, 12), 0.6, dtype="float32")
    vis = np.ones((10, 12), dtype="float32")
    vis[0:5, 0:6] = 0.0                              # half the field masked
    inv = Transformer.from_crs("EPSG:32643", "EPSG:4326", always_xy=True).transform
    poly = shp_transform(inv, _b(400000, 1899900, 400120, 1900000))

    png, meta = raster_io.render_ndvi_png(ndvi, vis, tr, "EPSG:32643", poly)
    im = np.array(Image.open(_io.BytesIO(png)))
    transparent = int((im[..., 3] == 0).sum())
    opaque = int((im[..., 3] == 255).sum())
    assert transparent > 0 and opaque > 0
    assert abs(transparent / (transparent + opaque) - 0.25) < 0.05   # quarter masked


def test_colour_ramp_matches_the_app_legend():
    """NDVI_STOPS is a copy of src/lib/ndviScience.ts NDVI_COLOR_STOPS. If the
    two drift the on-screen legend describes a picture it did not produce."""
    import raster_io
    assert raster_io.NDVI_STOPS[0] == (-0.20, "#7C3F1C")
    assert raster_io.NDVI_STOPS[-1] == (1.00, "#1B5E20")
    # exact stop values must reproduce their own hex
    for value, hex_ in raster_io.NDVI_STOPS:
        rgb = tuple(int(x) for x in raster_io.colorize(np.array([value]))[0])
        assert rgb == raster_io._hex_to_rgb(hex_), (value, hex_, rgb)


def test_storage_path_is_per_observation_and_tenant_first():
    """Tenant segment first, because the storage RLS policy authorises on
    (storage.foldername(name))[1]. One object per acquisition, so an August
    metric can never sit beside a June picture again."""
    import raster_io
    p = raster_io.storage_path("11111111-1111-1111-1111-111111111111",
                               "22222222-2222-2222-2222-222222222222",
                               "2026-08-21", "S2C_MSIL2A_20260821T052641_R105_T43QDU")
    assert p.startswith("11111111-1111-1111-1111-111111111111/")
    assert p.split("/")[1] == "22222222-2222-2222-2222-222222222222"
    assert p.endswith(".png") and "2026-08-21" in p
    # a scene id with awkward characters must not escape the folder
    weird = raster_io.storage_path("t", "l", "2026-08-21", "../../etc/passwd")
    assert ".." not in weird.split("/")[-1] and weird.count("/") == 2


# ===========================================================================
# v3.4 TILE-SCENE BATCHED READS — accuracy must be bit-identical
# ===========================================================================
def _synthetic_scene(tmpdir):
    """One synthetic Sentinel-2-like scene with a cloud patch, on disk."""
    import types, datetime as dt, rasterio
    from rasterio.transform import from_origin
    from shapely.geometry import box as _b, mapping
    from shapely.ops import transform as shp_transform
    from pyproj import Transformer
    crs = "EPSG:32643"; T10 = from_origin(400000, 1900000, 10, 10); T20 = from_origin(400000, 1900000, 20, 20)
    rng = np.random.default_rng(5)

    def w(p, a, tr, d, nd):
        with rasterio.open(p, 'w', driver='GTiff', height=a.shape[0], width=a.shape[1], count=1,
                           dtype=d, crs=crs, transform=tr, nodata=nd) as f:
            f.write(a, 1)
    for k, b in (("B02", 1300), ("B03", 1650), ("B04", 1750), ("B08", 5400)):
        w(f"{tmpdir}/{k}.tif", (b + rng.integers(-200, 200, (150, 150))).astype("uint16"), T10, "uint16", 0)
    for k, b in (("B05", 2600), ("B8A", 5100), ("B11", 2250)):
        w(f"{tmpdir}/{k}.tif", (b + rng.integers(-150, 150, (75, 75))).astype("uint16"), T20, "uint16", 0)
    scl = np.full((75, 75), 4, "uint8"); scl[8:16, 8:16] = 9          # cloud, will be dilated
    w(f"{tmpdir}/SCL.tif", scl, T20, "uint8", 0)
    inv = Transformer.from_crs(crs, "EPSG:4326", always_xy=True).transform
    item = types.SimpleNamespace(
        id="S_TEST", datetime=dt.datetime(2026, 9, 12, tzinfo=dt.timezone.utc),
        properties={"s2:processing_baseline": "05.12", "eo:cloud_cover": 5, "platform": "S2C",
                    "s2:mgrs_tile": "43QDU", "sat:relative_orbit": 105},
        geometry=mapping(shp_transform(inv, _b(390000, 1890000, 410000, 1910000))),
        assets={k: types.SimpleNamespace(href=f"{tmpdir}/{k}.tif", extra_fields={})
                for k in ("B02", "B03", "B04", "B05", "B8A", "B08", "B11", "SCL")})
    parcels = [shp_transform(inv, _b(400100 + dx, 1899100 + dy, 400100 + dx + sx, 1899100 + dy + sy))
               for dx, dy, sx, sy in [(0, 0, 120, 90), (250, 180, 70, 70), (500, 350, 160, 130)]]
    return item, parcels


def test_batched_block_reads_match_per_parcel_reads(tmp_path):
    """A block read sliced per parcel must equal read_band for that parcel -
    same values, same coverage fractions (EPC and purity come from these),
    same transform, same dilated SCL. If this drifts, every statistic drifts."""
    from shapely.ops import unary_union
    from raster_utils import read_band
    import tile_reader
    item, parcels = _synthetic_scene(str(tmp_path))
    bands = ["B04", "B02", "B03", "B08", "B05", "B8A", "B11", "SCL"]
    block = tile_reader.read_scene_block(item, bands, unary_union(parcels))
    assert set(block.bands) == set(bands)

    for geom in parcels:
        a = read_band(item, "B04", geom)
        b = tile_reader.subset_band(block, "B04", geom)
        assert b is not None
        assert a[1] == b[1]                                   # identical transform
        assert np.allclose(np.nan_to_num(a[0], nan=-999), np.nan_to_num(b[0], nan=-999))
        assert np.allclose(a[3], b[3])                        # coverage -> EPC, purity
        ref = (a[0].shape, a[1], a[2], a[3])
        for bk in ("B02", "B03", "B08", "B05", "B8A", "B11"):
            x = read_band(item, bk, geom, reference=ref)[0]
            y = tile_reader.subset_band(block, bk, geom, reference=ref)[0]
            assert np.allclose(np.nan_to_num(x, nan=-999), np.nan_to_num(y, nan=-999)), bk
        xs = read_band(item, "SCL", geom, reference=ref, categorical=True)[0]
        ys = tile_reader.subset_band(block, "SCL", geom, reference=ref, categorical=True)[0]
        assert np.array_equal(xs, ys)                         # dilated cloud mask


def test_batched_process_land_produces_identical_rows(tmp_path):
    """Whole-field statistics must be unchanged by batching, and the batched
    path must issue no per-parcel reads at all."""
    from shapely.geometry import mapping
    from shapely.ops import unary_union
    import raster_utils, processor, tile_reader
    item, parcels = _synthetic_scene(str(tmp_path))
    lands = [{"id": f"L{i}", "tenant_id": "T", "area_acres": 1.0,
              "boundary_geom": mapping(p), "ndvi_thumbnail_url": None} for i, p in enumerate(parcels)]
    keys = ("ndvi_value", "ndre_value", "ndmi_value", "effective_pixel_count",
            "coverage_weighted_purity", "ndvi_spatial_min", "ndvi_spatial_max",
            "uniformity_cv", "quality_score", "evidence_confidence", "valid_pixels", "total_pixels")

    calls = {"n": 0}
    orig = raster_utils.read_band

    def counted(*a, **k):
        calls["n"] += 1
        return orig(*a, **k)
    processor.read_band = counted
    try:
        per_land = [[{k: r.get(k) for k in keys} for r in processor.process_land(l, scenes=[item], history=[])[0]]
                    for l in lands]
        reads_per_land = calls["n"]

        calls["n"] = 0
        bands = ["B04"] + [b for b in processor.S2_BANDS_10M if b != "B04"] + list(processor.S2_BANDS_20M) + ["SCL"]
        blocks = {item.id: tile_reader.read_scene_block(item, bands, unary_union(parcels))}
        batched = [[{k: r.get(k) for k in keys} for r in processor.process_land(l, scenes=[item], history=[], blocks=blocks)[0]]
                   for l in lands]
        assert batched == per_land, "batching changed a measured value"
        assert reads_per_land > 0 and calls["n"] == 0, "batched path still issued per-parcel reads"
    finally:
        processor.read_band = orig


# ===========================================================================
# v3.4 MEMORY BOUND — the release audit's main technical objection
# ===========================================================================
def test_raster_cache_budget_is_process_wide_and_releases():
    """The cap must be on TOTAL bytes held at once across all workers, not on
    one scene. When it is exhausted, caching stops (callers fall back to
    reading) and nothing raises."""
    from resource_budget import ByteBudget
    b = ByteBudget(10 * 1024 * 1024)                      # 10 MB
    assert b.acquire(6 * 1024 * 1024) is True
    assert b.acquire(6 * 1024 * 1024) is False            # would exceed the ceiling
    assert b.denied == 1
    b.release(6 * 1024 * 1024)
    assert b.acquire(6 * 1024 * 1024) is True             # space returned
    assert b.peak_bytes <= b.limit


def test_raster_cache_budget_is_thread_safe():
    """Ten workers competing must never push usage past the limit."""
    import threading
    from resource_budget import ByteBudget
    b = ByteBudget(10 * 1024 * 1024)
    granted = []

    def worker():
        if b.acquire(1024 * 1024):
            granted.append(1)
    threads = [threading.Thread(target=worker) for _ in range(50)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(granted) == 10                             # exactly the budget, no more
    assert b.peak_bytes <= b.limit


def test_read_error_classification_separates_throttling():
    """429 and 5xx/timeout must be counted apart from ordinary failures, or a
    concurrency canary cannot tell throttling from a real error."""
    from resource_budget import Counters
    c = Counters()
    c.classify_read_error(RuntimeError("HTTP 429 Too Many Requests"))
    c.classify_read_error(RuntimeError("503 Service Unavailable"))
    c.classify_read_error(RuntimeError("connection timed out"))
    c.classify_read_error(ValueError("bad geometry"))
    snap = c.snapshot()
    assert snap["read_errors_429"] == 1
    assert snap["read_errors_5xx_or_timeout"] == 2
    assert snap["read_errors_other"] == 1
    assert "raster_cache_peak_mb" in snap and "raster_cache_limit_mb" in snap



def test_batched_scl_identical_when_cloud_touches_parcels(tmp_path):
    """The earlier equivalence test placed cloud AWAY from every parcel, so it
    only proved the clear-sky case. This one puts cloud INSIDE a parcel, a
    cloud 5 m OUTSIDE a parcel whose dilation crosses its boundary, and shadow
    and cloud at a parcel edge that is also the block edge. SCL, every mask,
    accept/reject and the measured values must all match the per-parcel path."""
    import types, datetime as dt, rasterio
    from rasterio.transform import from_origin
    from shapely.geometry import box as _b, mapping
    from shapely.ops import transform as shp_transform, unary_union
    from pyproj import Transformer
    from raster_utils import read_band, scl_masks
    import tile_reader, processor
    d = str(tmp_path); crs = "EPSG:32643"
    T10 = from_origin(400000, 1900000, 10, 10); T20 = from_origin(400000, 1900000, 20, 20)
    rng = np.random.default_rng(9)

    def w(p, a, tr, dd, nd):
        with rasterio.open(p, 'w', driver='GTiff', height=a.shape[0], width=a.shape[1], count=1,
                           dtype=dd, crs=crs, transform=tr, nodata=nd) as f:
            f.write(a, 1)
    for k, b in (("B02", 1300), ("B03", 1650), ("B04", 1750), ("B08", 5400)):
        w(f"{d}/{k}.tif", (b + rng.integers(-200, 200, (150, 150))).astype("uint16"), T10, "uint16", 0)
    for k, b in (("B05", 2600), ("B8A", 5100), ("B11", 2250)):
        w(f"{d}/{k}.tif", (b + rng.integers(-150, 150, (75, 75))).astype("uint16"), T20, "uint16", 0)
    scl = np.full((75, 75), 4, "uint8")
    px = lambda x, y: (int((1900000 - y) // 20), int((x - 400000) // 20))
    r, c = px(400140, 1899150); scl[r - 1:r + 2, c - 1:c + 2] = 9     # cloud INSIDE parcel 0
    r, c = px(400345, 1899315); scl[r, c] = 9                         # 5 m outside parcel 1
    r, c = px(400770, 1899500); scl[r, c] = 3                         # shadow at block edge
    r, c = px(400680, 1899600); scl[r, c] = 8                         # cloud at parcel 2 north edge
    w(f"{d}/SCL.tif", scl, T20, "uint8", 0)
    inv = Transformer.from_crs(crs, "EPSG:4326", always_xy=True).transform
    item = types.SimpleNamespace(
        id="S_HARD", datetime=dt.datetime(2026, 9, 12, tzinfo=dt.timezone.utc),
        properties={"s2:processing_baseline": "05.12", "eo:cloud_cover": 9, "platform": "S2C",
                    "s2:mgrs_tile": "43QDU", "sat:relative_orbit": 105},
        geometry=mapping(shp_transform(inv, _b(390000, 1890000, 410000, 1910000))),
        assets={k: types.SimpleNamespace(href=f"{d}/{k}.tif", extra_fields={})
                for k in ("B02", "B03", "B04", "B05", "B8A", "B08", "B11", "SCL")})
    parcels = [shp_transform(inv, _b(400100 + dx, 1899100 + dy, 400100 + dx + sx, 1899100 + dy + sy))
               for dx, dy, sx, sy in [(0, 0, 120, 90), (250, 180, 70, 70), (500, 350, 160, 130)]]
    block = tile_reader.read_scene_block(item, ["B04", "B02", "B03", "B08", "B05", "B8A", "B11", "SCL"],
                                         unary_union(parcels))
    touched = 0
    for g in parcels:
        a = read_band(item, "B04", g); ref = (a[0].shape, a[1], a[2], a[3])
        xs = read_band(item, "SCL", g, reference=ref, categorical=True)[0]
        ys = tile_reader.subset_band(block, "SCL", g, reference=ref, categorical=True)[0]
        assert np.array_equal(xs, ys)
        mx, my = scl_masks(xs, coverage=a[3]), scl_masks(ys, coverage=a[3])
        for k in mx:
            if isinstance(mx[k], np.ndarray):
                assert np.array_equal(mx[k], my[k]), k
        touched += int(np.sum(np.isin(xs, [3, 8, 9, 10]) & (a[3] > 0)) > 0)
    assert touched == 3, "every parcel must actually be touched by cloud/shadow"
    keys = ("ndvi_value", "cloud_cover", "valid_pixels", "effective_pixel_count",
            "coverage_weighted_purity", "quality_score", "evidence_confidence")
    lands = [{"id": f"L{i}", "tenant_id": "T", "area_acres": 1.0, "boundary_geom": mapping(p),
              "ndvi_thumbnail_url": None} for i, p in enumerate(parcels)]
    per = [[{k: r.get(k) for k in keys} for r in processor.process_land(l, scenes=[item], history=[])[0]] for l in lands]
    bat = [[{k: r.get(k) for k in keys} for r in processor.process_land(l, scenes=[item], history=[],
            blocks={item.id: block})[0]] for l in lands]
    assert per == bat


# ===========================================================================
# v3.4 INCREMENTAL PROCESSING — skip what was already measured, lose nothing
# ===========================================================================
def test_incremental_nights_skip_known_scenes_and_lose_nothing(tmp_path, monkeypatch):
    """Five nights against an in-memory ledger and database:
      1. first run measures everything and records outcomes
      2. nothing new -> every land 'unchanged', ZERO raster reads
      3. one new scene -> only it is read; values equal a full re-measurement
      4. a late-arriving OLDER scene -> measured, but the snapshot never moves back
      5. a redrawn boundary -> that land re-evaluates every scene, via block reads
    """
    import types, datetime as dt, rasterio
    from datetime import datetime, timezone
    from rasterio.transform import from_origin
    from shapely.geometry import box as _b, mapping
    from shapely.ops import transform as shp_transform, unary_union
    from pyproj import Transformer
    import raster_utils, processor, main, tile_reader
    crs = "EPSG:32643"; T10 = from_origin(400000, 1900000, 10, 10); T20 = from_origin(400000, 1900000, 20, 20)
    inv = Transformer.from_crs(crs, "EPSG:4326", always_xy=True).transform

    def scene(name, sid, day, cloudy=False, seed=0):
        d = tmp_path / name; d.mkdir(); rng = np.random.default_rng(seed)

        def w(p, a, tr, dd, nd):
            with rasterio.open(p, 'w', driver='GTiff', height=a.shape[0], width=a.shape[1], count=1,
                               dtype=dd, crs=crs, transform=tr, nodata=nd) as f:
                f.write(a, 1)
        for k, b in (("B02", 1300), ("B03", 1650), ("B04", 1750), ("B08", 5400)):
            w(f"{d}/{k}.tif", (b + rng.integers(-200, 200, (150, 150))).astype("uint16"), T10, "uint16", 0)
        for k, b in (("B05", 2600), ("B8A", 5100), ("B11", 2250)):
            w(f"{d}/{k}.tif", (b + rng.integers(-150, 150, (75, 75))).astype("uint16"), T20, "uint16", 0)
        w(f"{d}/SCL.tif", np.full((75, 75), 9 if cloudy else 4, "uint8"), T20, "uint8", 0)
        return types.SimpleNamespace(
            id=sid, datetime=dt.datetime(2026, 9, day, tzinfo=dt.timezone.utc),
            properties={"s2:processing_baseline": "05.12", "eo:cloud_cover": 5, "platform": "S2C",
                        "s2:mgrs_tile": "43QDU", "sat:relative_orbit": 105},
            geometry=mapping(shp_transform(inv, _b(390000, 1890000, 410000, 1910000))),
            assets={k: types.SimpleNamespace(href=f"{d}/{k}.tif", extra_fields={})
                    for k in ("B02", "B03", "B04", "B05", "B8A", "B08", "B11", "SCL")})
    A = scene("a", "S2_A", 10, seed=1); B = scene("b", "S2_B", 15, cloudy=True, seed=2)
    C = scene("c", "S2_C", 20, seed=3); OLD = scene("o", "S2_OLD", 5, seed=4)
    polys = [shp_transform(inv, _b(400100 + dx, 1899100 + dy, 400100 + dx + sx, 1899100 + dy + sy))
             for dx, dy, sx, sy in [(0, 0, 120, 90), (250, 180, 70, 70)]]
    lands = [{"id": f"L{i}", "tenant_id": "T", "area_acres": 1.0, "boundary_geom": mapping(p),
              "ndvi_thumbnail_url": None} for i, p in enumerate(polys)]
    block_geom = unary_union(polys)

    LEDGER, NDVI, SNAP, STATUS = [], {}, {}, {}

    def fetch(ids, ver, since):
        out = {}
        for r in LEDGER:
            if r["land_id"] in ids and r["pipeline_version"] == ver:
                out.setdefault(r["land_id"], {}).setdefault(r["scene_id"], []).append(
                    (r["geometry_fingerprint"], r["outcome"]))
        return out

    def record(rows):
        key = ("land_id", "scene_id", "pipeline_version", "geometry_fingerprint")
        for r in rows:
            LEDGER[:] = [x for x in LEDGER if not all(x[k] == r[k] for k in key)]
            LEDGER.append(r)
        return len(rows)

    def upsert(rows, run_started_at=None):
        for r in rows:
            NDVI[(r["land_id"], r["scene_id"])] = r
        return len(rows), len(rows)

    def history(land_id, days):
        return sorted([{"acquisition_date": r["acquisition_date"], "ndvi_value": r["ndvi_value"],
                        "scene_id": r["scene_id"]} for (l, _), r in NDVI.items() if l == land_id],
                      key=lambda h: h["acquisition_date"], reverse=True)
    monkeypatch.setattr(processor, "ENABLE_S1_FALLBACK", False)
    monkeypatch.setattr(main, "fetch_scene_ledger", fetch)
    monkeypatch.setattr(main, "record_scene_evaluations", record)

    def upsert_batch(rows, run_started_at=None):
        written = {}
        for r in rows:
            NDVI[(r["land_id"], r["scene_id"])] = r
            written[r["land_id"]] = written.get(r["land_id"], 0) + 1
        return written, dict(written), set()
    monkeypatch.setattr(main, "upsert_observations_batch", upsert_batch)
    monkeypatch.setattr(main, "optical_history_batch", lambda ids, days: {i: history(i, days) for i in ids})
    monkeypatch.setattr(main, "update_land_snapshot", lambda **k: SNAP.__setitem__(k["land_id"], k["acquisition_date"]))
    monkeypatch.setattr(main, "mark_land_status", lambda lid, st, msg: STATUS.__setitem__(lid, st))
    monkeypatch.setattr(main, "apply_land_updates_batch", lambda entries: None)
    monkeypatch.setattr(main, "log_steps_batch", lambda payloads: len(payloads))
    monkeypatch.setattr(main, "persist_observed_intelligence_batch",
                        lambda rows: ({r["land_id"]: 1 for r in rows}, {}))
    water = []
    monkeypatch.setattr(main, "process_land_water_layers",
                        lambda land, scenes=None, lookback_days=20, scene_bands=None, only_scene_ids=None,
                        sink=None, uploads=None: water.append(only_scene_ids) or 0)
    import raster_io
    monkeypatch.setattr(raster_io, "upload_png", lambda client, path, data: path)   # images upload fine here
    reads = {"parcel": 0, "block": 0}
    orig_impl, orig_block = raster_utils._read_band_impl, tile_reader.read_scene_block

    def c_impl(*a, **k):
        reads["parcel"] += 1
        return orig_impl(*a, **k)

    def c_block(*a, **k):
        reads["block"] += 1
        return orig_block(*a, **k)
    monkeypatch.setattr(raster_utils, "_read_band_impl", c_impl)
    monkeypatch.setattr(main, "read_scene_block", c_block)
    run = datetime.now(timezone.utc)

    def night(scenes, incremental=True):
        reads.update(parcel=0, block=0); water.clear()
        return main.handle_block(lands, block_geom, scenes, 20, run, incremental)

    night([A, B])                                                          # 1
    assert len(LEDGER) == 4                                                # 2 lands x (A accepted, B rejected)
    r2 = night([A, B])                                                     # 2
    assert [r["status"] for r in r2] == ["unchanged", "unchanged"]
    assert reads == {"parcel": 0, "block": 0}
    night([A, B, C])                                                       # 3
    assert reads["block"] == 1 and water == [["S2_C"], ["S2_C"]]
    inc = {k: v["ndvi_value"] for k, v in NDVI.items() if k[1] == "S2_C"}
    saved = dict(NDVI); NDVI.clear()
    night([A, B, C], incremental=False)
    assert {k: v["ndvi_value"] for k, v in NDVI.items() if k[1] == "S2_C"} == inc
    NDVI.clear(); NDVI.update(saved)
    before = dict(SNAP)
    night([OLD, A, B, C])                                                  # 4
    assert ("L0", "S2_OLD") in NDVI and SNAP == before                     # measured, snapshot not moved back
    lands[1]["boundary_geom"] = mapping(shp_transform(inv, _b(400350, 1899280, 400430, 1899360)))
    r5 = night([OLD, A, B, C])                                             # 5
    assert [r["status"] for r in r5] == ["unchanged", "completed"]
    assert reads["block"] == 4                                             # re-evaluated via block reads
    assert "no_data" not in STATUS.values()



def test_block_persistence_isolates_a_failed_land(monkeypatch):
    """If one land's observations fail to store, its neighbours still complete,
    and the failed land gets NO snapshot and NO 'accepted' ledger entry - so it
    is simply measured again next night. Batching must never trade isolation."""
    from datetime import datetime, timezone
    import main
    SNAP, STATUS = {}, {}
    monkeypatch.setattr(main, "update_land_snapshot", lambda **k: SNAP.__setitem__(k["land_id"], k["acquisition_date"]))
    monkeypatch.setattr(main, "mark_land_status", lambda lid, st, msg: STATUS.__setitem__(lid, st))
    monkeypatch.setattr(main, "apply_land_updates_batch", lambda entries: None)
    monkeypatch.setattr(main, "log_steps_batch", lambda payloads: len(payloads))
    monkeypatch.setattr(main, "persist_observed_intelligence_batch",
                        lambda rows: ({r["land_id"]: 1 for r in rows}, {}))

    def upsert_batch(rows, run_started_at=None):
        written = {r["land_id"]: 1 for r in rows if r["land_id"] != "BAD"}
        return written, dict(written), {"BAD"}
    monkeypatch.setattr(main, "upsert_observations_batch", upsert_batch)

    def pending(lid):
        row = {"land_id": lid, "tenant_id": "T", "scene_id": f"S_{lid}", "acquisition_date": "2026-09-20",
               "observation_source": "sentinel-2", "ndvi_value": 0.7, "quality_score": 0.9, "metadata": {}}
        return {"land": {"id": lid, "tenant_id": "T"}, "started": datetime.now(timezone.utc),
                "result": main._new_result(lid), "logs": [], "history": [], "rows": [row],
                "report": {"geometry_fingerprint": "fp", "optical_rejects": []},
                "context_report": {}, "error": None}
    res = {r["land_id"]: r for r in main.persist_block([pending("OK1"), pending("BAD"), pending("OK2")],
                                                        20, datetime.now(timezone.utc))}
    assert res["OK1"]["status"] == res["OK2"]["status"] == "completed"
    assert res["BAD"]["status"] == "failed"
    assert "BAD" not in SNAP and set(SNAP) == {"OK1", "OK2"}
    assert STATUS.get("BAD") == "failed"
    assert not any(e["outcome"] == "accepted" for e in res["BAD"]["scene_evaluations"])
    assert any(e["outcome"] == "accepted" for e in res["OK1"]["scene_evaluations"])


# ===========================================================================
# v3.4 STREAMED ORCHESTRATION, SPATIAL GROUPING, RUN-HEALTH GUARDS
# ===========================================================================
def _real_centres():
    """Centres of the 30 live lands (2026-09-23), ids truncated. 28 of them
    had no stored tile id and were processed alone before this change."""
    import json, os
    raw = json.load(open(os.path.join(os.path.dirname(__file__), "fixture_real_land_centres.json")))
    return [{"id": i, "tenant_id": "T", "tile_id": t, "mgrs_tile_id": None,
             "center_lat": la, "center_lon": lo} for i, t, la, lo in raw]


def test_spatial_grouping_leaves_no_land_alone_on_real_centres():
    from tile_grouping import group_lands_by_tile
    g = group_lands_by_tile(_real_centres())
    assert "__untiled__" not in g
    assert max(len(v) for v in g.values()) == 28          # the Kolhapur cluster shares one group
    assert sum(len(v) for v in g.values()) == 30


def test_spatial_group_key_is_deterministic_and_exact():
    from tile_grouping import spatial_group_key
    a = spatial_group_key({"center_lat": 16.8636, "center_lon": 74.3095})
    assert a == spatial_group_key({"center_lat": 16.8636, "center_lon": 74.3095})
    assert a.startswith("utm43N:")
    assert spatial_group_key({"center_lat": None, "center_lon": None}) is None


def _run_main_with(monkeypatch, status):
    import sys, threading, time
    from shapely.geometry import box, mapping
    import main
    light = _real_centres()
    full = {l["id"]: {**l, "boundary_geom": mapping(box(l["center_lon"] - 5e-4, l["center_lat"] - 5e-4,
                                                         l["center_lon"] + 5e-4, l["center_lat"] + 5e-4))}
            for l in light}
    seen, peak, now, lock = [], {"v": 0}, {"v": 0}, threading.Lock()

    def fake_block(block_lands, block_geom, scenes, lookback, run_started, incremental=True):
        with lock:
            now["v"] += 1; peak["v"] = max(peak["v"], now["v"]); seen.extend(l["id"] for l in block_lands)
        time.sleep(0.005)
        with lock:
            now["v"] -= 1
        return [{"land_id": l["id"], "rows": 0, "new_rows": 0, "source": None,
                 "status": status, "error": None} for l in block_lands]
    monkeypatch.setattr(main, "iter_land_keys", lambda tenant=None: iter(light))
    monkeypatch.setattr(main, "fetch_lands_by_ids", lambda ids: [full[i] for i in ids])
    monkeypatch.setattr(main, "scenes_for_group", lambda lands, lookback_days=None: [])
    monkeypatch.setattr(main, "handle_block", fake_block)
    monkeypatch.setattr(main, "count_eligible_lands", lambda tenant=None: len(light))
    monkeypatch.setattr(main, "write_run_summary", lambda s: None)
    monkeypatch.setattr(sys, "argv", ["main.py"])
    return main.main(), seen, peak["v"], main.TILE_WORKERS


def test_orchestration_processes_every_land_once_with_bounded_concurrency(monkeypatch):
    rc, seen, peak, workers = _run_main_with(monkeypatch, "unchanged")
    assert sorted(seen) == sorted(l["id"] for l in _real_centres())
    assert peak <= workers


def test_quiet_night_is_healthy_but_outage_still_fails(monkeypatch):
    """With incremental processing a night with no new pass is normal and must
    not turn the job red; a real outage (no scenes, so nothing can be
    'unchanged') must still fail with exit code 2."""
    rc_quiet, *_ = _run_main_with(monkeypatch, "unchanged")
    assert rc_quiet == 0
    rc_outage, *_ = _run_main_with(monkeypatch, "skipped")
    assert rc_outage == 2


# ===========================================================================
# v3.2 AUDIT FIXES (2026-09-23 run log + live database)
# ===========================================================================
def _s1_item(sid, day, geom):
    import types, datetime as dt
    from shapely.geometry import mapping
    return types.SimpleNamespace(id=sid, datetime=dt.datetime(2026, 9, day, tzinfo=dt.timezone.utc),
                                 properties={}, geometry=mapping(geom), assets={"vv": None, "vh": None})


def test_search_s1_returns_newest_first_whatever_the_server_order(monkeypatch):
    """Planetary Computer does not advertise STAC sort support, and the radar
    fallback takes the FIRST pair as the newest scene."""
    from shapely.geometry import box, mapping
    import sentinel_search
    near = box(74.0, 16.5, 74.6, 17.2)

    class _S:
        def items(self):
            return [_s1_item("OLD", 1, near), _s1_item("NEW", 13, near), _s1_item("MID", 7, near)]
    monkeypatch.setattr(sentinel_search, "client", lambda: type("C", (), {"search": lambda self, **k: _S()})())
    assert [p[1].id for p in sentinel_search.search_s1(mapping(box(74.3, 16.86, 74.31, 16.87)))] == ["NEW", "MID", "OLD"]


def test_s1_block_search_footprint_skip_and_fallback(monkeypatch):
    from shapely.geometry import box
    import processor
    field = box(74.30, 16.86, 74.31, 16.87)
    shared = lambda: [("rtc", _s1_item("FAR_NEWEST", 14, box(70, 10, 70.5, 10.5))),
                      ("rtc", _s1_item("COVERS", 12, box(74.0, 16.5, 74.6, 17.2)))]
    chosen = {}
    orig_meta = processor.acquisition_meta
    monkeypatch.setattr(processor, "acquisition_meta", lambda it: (chosen.__setitem__("id", it.id), orig_meta(it))[1])

    def stop(*a, **k):
        raise RuntimeError("stop")
    monkeypatch.setattr(processor, "read_band", stop)
    try:
        processor._process_s1({"id": "L", "tenant_id": "T"}, field, False, "high", 1e3,
                              {"geometry_fingerprint": "fp"}, 1e3, ledger={}, s1_search=shared)
    except Exception:
        pass
    assert chosen["id"] == "COVERS"                       # never a scene that misses the field
    reads = []
    monkeypatch.setattr(processor, "read_band", lambda *a, **k: reads.append(1))
    rep = {"geometry_fingerprint": "fp"}
    assert processor._process_s1({"id": "L", "tenant_id": "T"}, field, False, "high", 1e3, rep, 1e3,
                                 ledger={"COVERS": [("fp", "accepted")]}, s1_search=shared) == []
    assert reads == [] and rep["prior_accepted_in_window"] is True   # known radar scene: zero reads
    called = []
    monkeypatch.setattr(processor, "search_s1", lambda g, **k: called.append(1) or [])
    processor._process_s1({"id": "L", "tenant_id": "T"}, field, False, "high", 1e3,
                          {"geometry_fingerprint": "fp"}, 1e3, ledger={}, s1_search=lambda: None)
    assert called == [1]                                  # failed block search -> own search


def test_supabase_client_is_one_per_thread(monkeypatch):
    import threading
    import db
    made = []
    monkeypatch.setattr(db, "create_client", lambda u, k: made.append(object()) or made[-1])
    proxy = db._ThreadLocalClient("u", "k")
    got = []

    def work():
        got.append(proxy._client())
        assert proxy._client() is got[-1]                 # reused within a thread
    ts = [threading.Thread(target=work) for _ in range(4)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert len(made) == 4 and len({id(c) for c in got}) == 4


def test_upload_png_retries_transient_resets(monkeypatch):
    import raster_io
    monkeypatch.setattr("time.sleep", lambda s: None)
    calls = {"n": 0}

    class Store:
        def from_(self, b):
            return self

        def upload(self, **k):
            calls["n"] += 1
            if calls["n"] < 3:
                raise RuntimeError("RemoteProtocolError: Server disconnected")
    client = type("F", (), {"storage": Store()})()
    assert raster_io.upload_png(client, "t/l/x.png", b"png") == "t/l/x.png" and calls["n"] == 3
    calls["n"] = -100
    assert raster_io.upload_png(client, "t/l/y.png", b"png") is None      # never fails the land


def test_large_field_now_gets_a_real_neighbour_ring(tmp_path):
    """Under the old total-area rule every field >= 1 acre got a ring of zero
    width (all 66 'failures' on 2026-09-23). Every field size now gets a ring."""
    import types, datetime as dt, rasterio
    from rasterio.transform import from_origin
    from shapely.geometry import box as _b, mapping
    from shapely.ops import transform as shp_transform
    from pyproj import Transformer
    import parcel_context as pc
    crs = "EPSG:32643"; T10 = from_origin(400000, 1900000, 10, 10); T20 = from_origin(400000, 1900000, 20, 20)
    rng = np.random.default_rng(3)

    def w(p, a, tr, dd, nd):
        with rasterio.open(p, 'w', driver='GTiff', height=a.shape[0], width=a.shape[1], count=1,
                           dtype=dd, crs=crs, transform=tr, nodata=nd) as f:
            f.write(a, 1)
    for k, b in (("B04", 1700), ("B08", 5200)):
        w(f"{tmp_path}/{k}.tif", (b + rng.integers(-300, 300, (120, 120))).astype("uint16"), T10, "uint16", 0)
    w(f"{tmp_path}/SCL.tif", np.full((60, 60), 4, "uint8"), T20, "uint8", 0)
    item = types.SimpleNamespace(id="S_BIG", datetime=dt.datetime(2026, 9, 12, tzinfo=dt.timezone.utc),
                                 properties={"s2:processing_baseline": "05.12"},
                                 assets={k: types.SimpleNamespace(href=f"{tmp_path}/{k}.tif", extra_fields={})
                                         for k in ("B04", "B08", "SCL")})
    inv = Transformer.from_crs(crs, "EPSG:4326", always_xy=True).transform
    big = shp_transform(inv, _b(400300, 1899300, 400780, 1899780))    # 23 ha, like the largest real field
    r = pc.extract_parcel_context(item, big)
    assert r["status"] == "observed_context" and r["context_tier"] == "real"
    assert r["context_clean_pixels"] >= 20 and r["context_buffer_m"] <= 150


def _b_utm(w, h):
    from shapely.geometry import box
    return box(400000, 1899000, 400000 + w, 1899000 + h)


def test_sub_pixel_context_publishes_no_neighbour_verdict():
    """A context below MIN_EPC must reach the app with no z and no delta: on
    2026-09-23 a 15 m2 context produced z -1.88 and a 'behind' verdict."""
    import ndvi_intelligence_writer as w
    row = {"land_id": "L", "tenant_id": "T", "scene_id": "S", "acquisition_date": "2026-09-15",
           "acquisition_time": None, "observation_source": "sentinel-2", "ndvi_value": 0.6,
           "quality_score": 0.9, "metadata": {"parcel_context": {
               "status": "insufficient_context", "context_effective_pixel_count": 0.149, "min_epc": 3.0}}}
    rec = w.build_observed_intelligence(row)
    assert rec["parcel_context_robust_z"] is None and rec["parcel_context_delta"] is None


def test_forecast_log_step_call_matches_the_real_signature():
    """The positional call raised TypeError on the last line of every forecast."""
    import inspect, re
    import db
    src = open(inspect.getsourcefile(__import__("ndvi_temporal_predictor"))).read()
    assert 'log_step("INTELLIGENCE_FORECAST"' not in src
    assert re.search(r'log_step\(processing_step="INTELLIGENCE_FORECAST"', src)
    inspect.signature(db.log_step).bind(processing_step="INTELLIGENCE_FORECAST", step_status="completed",
                                        tenant_id=None, started_at=None, metadata={})


def test_forecast_runs_after_pipeline_and_fails_loudly():
    import os, yaml
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    wf = yaml.safe_load(open(os.path.join(root, ".github/workflows/ndvi-forecast.yml")))
    on = wf.get(True) or wf["on"]
    assert "schedule" not in on and on["workflow_run"]["workflows"] == ["NDVI Pipeline"]
    run = [s for s in wf["jobs"]["forecast"]["steps"] if "ndvi_temporal_predictor" in str(s.get("run", ""))][0]["run"]
    assert "set -o pipefail" in run


# ===== zone map, where-to-check quarters, one-time thumbnail (merged 2026-09-23) =====
def test_zone_watch_then_check_on_persistence():
    from zone_stats import compute_zones
    ndvi = np.full((8, 8), 0.70, "float32"); ndvi[4:, 4:] = 0.55
    ndre = np.full((8, 8), 0.40, "float32"); ndre[4:, 4:] = 0.30
    cov = np.ones((8, 8)); crop = np.ones((8, 8), bool)
    z1 = compute_zones({"NDVI": ndvi, "NDRE": ndre, "NDMI": np.full((8, 8), 0.25, "float32")}, cov.copy(), cov, crop, 0.70, 0.01, "high", None)
    assert z1["level"] == "watch" and z1["weakest"] == "SE"
    z2 = compute_zones({"NDVI": ndvi, "NDRE": ndre}, cov.copy(), cov, crop, 0.70, 0.01, "high", z1)
    assert z2["level"] == "check" and z2["persistent"] is True


def test_zone_refuses_small_or_uniform_fields():
    from zone_stats import compute_zones
    crop = np.ones((8, 8), bool)
    small = np.zeros((8, 8)); small[3:5, 3:5] = [[1, 1], [1, 0.5]]
    ndvi = np.full((8, 8), 0.70, "float32"); ndvi[4:, 4:] = 0.55
    assert compute_zones({"NDVI": ndvi}, np.where(small > 0, small, 0), small, crop, 0.70, 0.01, "high", None)["reason"] == "field_too_small"
    cov = np.ones((8, 8))
    assert compute_zones({"NDVI": np.full((8, 8), 0.7, "float32")}, cov.copy(), cov, crop, 0.70, 0.01, "high", None)["reason"] == "gap_within_noise"


def test_water_quarter_points_at_driest_part():
    from zone_stats import compute_zones
    ndvi = np.full((8, 8), 0.70, "float32")
    ndmi = np.full((8, 8), 0.25, "float32"); ndmi[:4, :4] = 0.12
    cov = np.ones((8, 8)); crop = np.ones((8, 8), bool)
    z = compute_zones({"NDVI": ndvi, "NDMI": ndmi}, cov.copy(), cov, crop, 0.70, 0.01, "high", None)
    assert z["water"]["weakest"] == "NW" and z["water"]["level"] == "watch"


def _utm_field():
    from rasterio.transform import from_origin
    from shapely.ops import transform as shp_transform
    from pyproj import Transformer
    from shapely.geometry import box as _b
    inv = Transformer.from_crs("EPSG:32643", "EPSG:4326", always_xy=True).transform
    return from_origin(400000, 1900000, 10, 10), shp_transform(inv, _b(400000, 1899900, 400120, 1900000))


def test_zone_map_uniform_field_is_one_colour():
    """A uniform healthy field must not get a red zone (why terciles are not used)."""
    import raster_io
    tr, poly = _utm_field()
    png, meta = raster_io.render_zone_png(np.full((10, 12), 0.70, "float32"), np.ones((10, 12)), tr, "EPSG:32643", poly, 0.70)
    assert meta["shares"]["lower"] == 0 and meta["shares"]["higher"] == 0 and meta["shares"]["normal"] == 1.0


def test_zone_map_marks_the_weak_patch_lower():
    import raster_io
    tr, poly = _utm_field()
    ndvi = np.full((10, 12), 0.70, "float32"); ndvi[5:, 6:] = 0.50
    png, meta = raster_io.render_zone_png(ndvi, np.ones((10, 12)), tr, "EPSG:32643", poly, 0.70)
    assert 0.15 < meta["shares"]["lower"] < 0.35 and meta["shares"]["higher"] == 0
    assert meta["crs"] == "EPSG:4326" and png[:8] == b"\x89PNG\r\n\x1a\n"


def test_truecolor_thumbnail_dims_context():
    import raster_io
    from rasterio.transform import from_origin
    from shapely.ops import transform as shp_transform
    from pyproj import Transformer
    from shapely.geometry import box as _b
    from PIL import Image
    import io as _io
    rng = np.random.default_rng(0)
    b = [(0.05 + 0.03 * rng.random((40, 40))).astype("float32") for _ in range(3)]
    cov = np.zeros((40, 40)); cov[12:28, 12:28] = 1
    inv = Transformer.from_crs("EPSG:32643", "EPSG:4326", always_xy=True).transform
    poly = shp_transform(inv, _b(400120, 1899720, 400280, 1899880))
    png, meta = raster_io.render_truecolor_png(b[0], b[1], b[2], cov, from_origin(400000, 1900000, 10, 10), "EPSG:32643", poly)
    alpha = set(np.unique(np.array(Image.open(_io.BytesIO(png)))[..., 3]).tolist())
    assert 255 in alpha and 140 in alpha and max(meta["width"], meta["height"]) <= 360



def test_failed_image_upload_never_loses_the_measurement(monkeypatch):
    """Release review FIX-3: a Storage failure must not fail the NDVI row. The
    row is stored WITHOUT its image, the scene stays out of the ledger (so it
    is retried next night), and a neighbour's image is unaffected."""
    from datetime import datetime, timezone
    import main, raster_io
    SNAP = {}
    monkeypatch.setattr(main, "update_land_snapshot", lambda **k: SNAP.__setitem__(k["land_id"], k))
    monkeypatch.setattr(main, "mark_land_status", lambda *a, **k: None)
    monkeypatch.setattr(main, "apply_land_updates_batch", lambda entries: None)
    monkeypatch.setattr(main, "log_steps_batch", lambda payloads: len(payloads))
    monkeypatch.setattr(main, "persist_observed_intelligence_batch", lambda rows: ({r["land_id"]: 1 for r in rows}, {}))
    stored = {}

    def upsert_batch(rows, run_started_at=None):
        for r in rows:
            stored[r["land_id"]] = dict(r)
        return {r["land_id"]: 1 for r in rows}, {r["land_id"]: 1 for r in rows}, set()
    monkeypatch.setattr(main, "upsert_observations_batch", upsert_batch)
    water_written = []
    monkeypatch.setattr(main, "upsert_water_layers_batch", lambda recs: water_written.extend(recs) or set())
    monkeypatch.setattr(raster_io, "upload_png", lambda client, path, data: None if "BAD" in path else path)

    def pending(lid):
        path = f"T/{lid}/2026-09-20_S.png"
        row = {"land_id": lid, "tenant_id": "T", "scene_id": "S", "acquisition_date": "2026-09-20",
               "observation_source": "sentinel-2", "ndvi_value": 0.71, "quality_score": 0.9,
               "image_url": path, "metadata": {"image": {"storage_path": path}},
               "_uploads": [{"path": path, "data": b"png", "kind": "ndvi"}]}
        water = {"land_id": lid, "scene_id": "S", "image_path": f"T/{lid}/water/x.png", "image_metadata": {}}
        return {"land": {"id": lid, "tenant_id": "T"}, "started": datetime.now(timezone.utc),
                "result": main._new_result(lid), "logs": [], "history": [], "rows": [row],
                "report": {"geometry_fingerprint": "fp", "optical_rejects": []}, "context_report": {},
                "error": None, "water_records": [water],
                "uploads": [{"path": water["image_path"], "data": b"png", "kind": "water"}]}
    res = {r["land_id"]: r for r in main.persist_block([pending("OK"), pending("BAD")], 20,
                                                        datetime.now(timezone.utc))}
    assert stored["BAD"]["ndvi_value"] == 0.71 and stored["BAD"]["image_url"] is None   # number kept, image cleared
    assert stored["BAD"]["metadata"]["image"]["upload_failed"] is True
    assert "_uploads" not in stored["BAD"]                                              # bytes never sent to the DB
    assert stored["OK"]["image_url"] == "T/OK/2026-09-20_S.png"                         # neighbour unaffected
    assert res["BAD"]["status"] == res["OK"]["status"] == "completed"
    assert res["BAD"]["scene_evaluations"] == []                                        # retried next night
    assert any(e["outcome"] == "accepted" for e in res["OK"]["scene_evaluations"])
    assert len(water_written) == 2                                                      # water numbers kept for both
    assert [w for w in water_written if w["land_id"] == "BAD"][0]["image_path"] is None



def test_land_updates_are_one_merged_entry_per_land_in_one_batch(monkeypatch):
    """Release review FIX-1: a block's land writes go out as ONE batch with
    ONE entry per land (a set-based UPDATE with duplicate ids is
    nondeterministic), later changes overriding earlier ones field by field."""
    import main
    calls = []
    monkeypatch.setattr(main, "apply_land_updates_batch", lambda entries: calls.append(entries) or len(entries))
    lu = main._LandUpdates()
    lu.snapshot(land_id="A", ndvi_value=0.7, acquisition_date="2026-09-20", quality_score=0.9, source="sentinel-2")
    lu.status("A", "failed", "intelligence persistence failed")        # later status wins
    lu.status("B", "no_data", "no usable acquisition")
    lu.thumbnail("C", "T/C/truecolor_2026-09-20.png")
    lu.flush()
    assert len(calls) == 1
    by = {e["id"]: e for e in calls[0]}
    assert set(by) == {"A", "B", "C"}
    assert by["A"]["has_snapshot"] and by["A"]["last_ndvi_value"] == 0.7 and by["A"]["ndvi_status"] == "failed"
    assert by["B"]["ndvi_status"] == "no_data" and not by["B"].get("has_snapshot")
    assert by["C"]["ndvi_thumbnail_url"].endswith(".png")


def test_land_updates_fall_back_per_land_without_the_batch_function(monkeypatch):
    import main
    snaps, stats, thumbs = [], [], []
    monkeypatch.setattr(main, "apply_land_updates_batch", lambda entries: None)
    monkeypatch.setattr(main, "update_land_snapshot", lambda **k: snaps.append(k))
    monkeypatch.setattr(main, "mark_land_status", lambda lid, st, note=None: stats.append((lid, st)))
    monkeypatch.setattr(main, "update_land_snapshot_thumbnail", lambda lid, p: thumbs.append(lid))
    lu = main._LandUpdates()
    lu.snapshot(land_id="A", ndvi_value=0.7, acquisition_date="2026-09-20", quality_score=0.9, source="sentinel-2")
    lu.status("B", "no_data", "none")
    lu.thumbnail("C", "p.png")
    lu.flush()
    assert [s["land_id"] for s in snaps] == ["A"] and stats == [("B", "no_data")] and thumbs == ["C"]


# ===========================================================================
# MANDATORY SCIENTIFIC EQUIVALENCE (release review): OLD per-land read vs NEW
# block read + subset, for the same land and scene, EVERY output compared.
# Tolerances (explicit):
#   integers, strings, booleans, None, CRS, transforms ........ exactly equal
#   floats and float arrays ..................................... |a-b| <= 1e-9
#   NaN positions in arrays ..................................... identical
# Only fields that are SUPPOSED to differ are excluded: wall-clock timings.
# ===========================================================================
_EQ_EXCLUDE = {"processing_duration_ms", "processed_at", "created_at", "updated_at"}


def _eq_walk(a, b, path, diffs):
    import math
    if path.split(".")[-1] in _EQ_EXCLUDE:
        return
    if isinstance(a, dict) and isinstance(b, dict):
        for k in set(a) | set(b):
            if k not in a or k not in b:
                diffs.append(f"{path}.{k}: present in only one path"); continue
            _eq_walk(a[k], b[k], f"{path}.{k}", diffs)
    elif isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
        if len(a) != len(b):
            diffs.append(f"{path}: length {len(a)} != {len(b)}"); return
        for i, (x, y) in enumerate(zip(a, b)):
            _eq_walk(x, y, f"{path}[{i}]", diffs)
    elif isinstance(a, np.ndarray) or isinstance(b, np.ndarray):
        a, b = np.asarray(a), np.asarray(b)
        if a.shape != b.shape:
            diffs.append(f"{path}: shape {a.shape} != {b.shape}"); return
        if a.dtype.kind in "fc":
            if not np.array_equal(np.isnan(a), np.isnan(b)):
                diffs.append(f"{path}: NaN positions differ"); return
            m = ~np.isnan(a)
            if m.any() and float(np.max(np.abs(a[m] - b[m]))) > 1e-9:
                diffs.append(f"{path}: max |diff| {float(np.max(np.abs(a[m]-b[m]))):.3g}")
        elif not np.array_equal(a, b):
            diffs.append(f"{path}: arrays differ")
    elif isinstance(a, float) or isinstance(b, float):
        if a is None or b is None:
            if a is not b:
                diffs.append(f"{path}: {a!r} vs {b!r}")
        elif not (math.isnan(a) and math.isnan(b)) and abs(float(a) - float(b)) > 1e-9:
            diffs.append(f"{path}: {a!r} vs {b!r}")
    elif isinstance(a, (bytes, bytearray)) or isinstance(b, (bytes, bytearray)):
        if a != b:
            diffs.append(f"{path}: image bytes differ")
    else:
        if a != b:
            diffs.append(f"{path}: {a!r} vs {b!r}")


def test_mandatory_equivalence_every_output_old_vs_block(tmp_path, monkeypatch):
    import types, datetime as dt, rasterio
    from rasterio.transform import from_origin
    from shapely.geometry import box as _b, mapping, Polygon
    from shapely.ops import transform as shp_transform, unary_union
    from pyproj import Transformer
    from raster_utils import read_band, scl_masks
    import processor, tile_reader, raster_io
    monkeypatch.setattr(processor, "ENABLE_S1_FALLBACK", False)
    crs = "EPSG:32643"; T10 = from_origin(400000, 1900000, 10, 10); T20 = from_origin(400000, 1900000, 20, 20)
    rng = np.random.default_rng(42)

    def w(p, a, tr, dd, nd):
        with rasterio.open(p, 'w', driver='GTiff', height=a.shape[0], width=a.shape[1], count=1,
                           dtype=dd, crs=crs, transform=tr, nodata=nd) as f:
            f.write(a, 1)

    def scene(name, sid, day, cloud_boxes):
        d = tmp_path / name; d.mkdir()
        for k, b in (("B02", 1300), ("B03", 1650), ("B04", 1750), ("B08", 5300)):
            w(f"{d}/{k}.tif", (b + rng.integers(-400, 400, (200, 200))).astype("uint16"), T10, "uint16", 0)
        for k, b in (("B05", 2600), ("B8A", 5100), ("B11", 2250)):
            w(f"{d}/{k}.tif", (b + rng.integers(-200, 200, (100, 100))).astype("uint16"), T20, "uint16", 0)
        scl = np.full((100, 100), 4, "uint8")
        for r0, r1, c0, c1, v in cloud_boxes:
            scl[r0:r1, c0:c1] = v
        w(f"{d}/SCL.tif", scl, T20, "uint8", 0)
        inv = Transformer.from_crs(crs, "EPSG:4326", always_xy=True).transform
        return types.SimpleNamespace(
            id=sid, datetime=dt.datetime(2026, 9, day, tzinfo=dt.timezone.utc),
            properties={"s2:processing_baseline": "05.12", "eo:cloud_cover": 7, "platform": "S2C",
                        "s2:mgrs_tile": "43QDU", "sat:relative_orbit": 105},
            geometry=mapping(shp_transform(inv, _b(390000, 1890000, 410000, 1910000))),
            assets={k: types.SimpleNamespace(href=f"{d}/{k}.tif", extra_fields={})
                    for k in ("B02", "B03", "B04", "B05", "B8A", "B08", "B11", "SCL")})
    # scene 1 clouds touch parcels; scene 2 has shadow at a block edge
    s1 = scene("s1", "EQ_S1", 12, [(52, 55, 12, 15, 9), (60, 61, 28, 29, 8)])
    s2 = scene("s2", "EQ_S2", 17, [(70, 71, 43, 44, 3)])
    inv = Transformer.from_crs(crs, "EPSG:4326", always_xy=True).transform
    P = lambda g: shp_transform(inv, g)
    parcels = {
        "tiny_5_guntha":   P(_b(400300, 1898900, 400322, 1898923)),
        "ten_guntha":      P(_b(400500, 1898800, 400532, 1898832)),
        "strip_4_to_1":    P(_b(400240, 1898700, 400256, 1898764)),
        "cloud_inside":    P(_b(400230, 1898880, 400330, 1898960)),
        "one_acre":        P(_b(400560, 1898560, 400624, 1898624)),
        "irregular":       P(Polygon([(400700, 1898500), (400790, 1898520), (400770, 1898600),
                                      (400720, 1898590)])),
        "block_edge":      P(_b(400820, 1898580, 400880, 1898640)),
        "large_5_acre":    P(_b(400100, 1898300, 400242, 1898442)),
    }
    lands = {n: {"id": n, "tenant_id": "T", "area_acres": 1.0, "boundary_geom": mapping(g),
                 "ndvi_thumbnail_url": "x"} for n, g in parcels.items()}
    bands = ["B04"] + [b for b in processor.S2_BANDS_10M if b != "B04"] + list(processor.S2_BANDS_20M) + ["SCL"]
    union = unary_union(list(parcels.values()))
    blocks = {s.id: tile_reader.read_scene_block(s, bands, union, extra_pad_m=tile_reader.CONTEXT_BLOCK_PAD_M)
              for s in (s1, s2)}
    report_lines, all_diffs, compared = [], [], 0
    for name, g in parcels.items():
        # (1) raw raster contract: every band, CRS, transform, coverage, SCL
        for s in (s1, s2):
            a = read_band(s, "B04", g); b = tile_reader.subset_band(blocks[s.id], "B04", g)
            d = []
            _eq_walk({"arr": a[0], "transform": tuple(a[1]), "crs": str(a[2]), "coverage": a[3]},
                     {"arr": b[0], "transform": tuple(b[1]), "crs": str(b[2]), "coverage": b[3]}, f"{name}.{s.id}.B04", d)
            ref = (a[0].shape, a[1], a[2], a[3])
            for bk in bands[1:]:
                cat = bk == "SCL"
                x = read_band(s, bk, g, reference=ref, categorical=cat)[0]
                y = tile_reader.subset_band(blocks[s.id], bk, g, reference=ref, categorical=cat)[0]
                _eq_walk(x, y, f"{name}.{s.id}.{bk}", d)
                if cat:
                    _eq_walk(scl_masks(x, coverage=a[3]), scl_masks(y, coverage=a[3]), f"{name}.{s.id}.masks", d)
            all_diffs += d; compared += 1
        # (2) the full scientific output: every field of every row
        per = processor.process_land(lands[name], scenes=[s1, s2], history=[])
        bat = processor.process_land(lands[name], scenes=[s1, s2], history=[], blocks=blocks)
        d = []
        rows_a = sorted(per[0], key=lambda r: r["scene_id"]); rows_b = sorted(bat[0], key=lambda r: r["scene_id"])
        _eq_walk([{k: v for k, v in r.items() if k != "_uploads"} for r in rows_a],
                 [{k: v for k, v in r.items() if k != "_uploads"} for r in rows_b], f"{name}.rows", d)
        _eq_walk([[u["data"] for u in r.get("_uploads", [])] for r in rows_a],
                 [[u["data"] for u in r.get("_uploads", [])] for r in rows_b], f"{name}.images", d)
        _eq_walk(per[1].get("optical_rejects"), bat[1].get("optical_rejects"), f"{name}.rejects", d)
        all_diffs += d
        report_lines.append(f"{name}: {len(rows_a)} accepted rows, {len(per[1].get('optical_rejects', []))} rejected")
    assert compared == 16
    assert not all_diffs, "\n".join(all_diffs[:25])


# ===========================================================================
# RADAR ON EVERY PASS + BLOCK-SHARED VV/VH (2026-09-24 audit: radar frozen at
# 13 Sept; 15 of 30 lands with no reading newer than August)
# ===========================================================================
def _radar_scene(tmp_path, sid="S1_T", day=19):
    import types, datetime as dt, rasterio
    from rasterio.transform import from_origin
    from shapely.geometry import box as _b, mapping
    from shapely.ops import transform as shp_transform
    from pyproj import Transformer
    crs = "EPSG:32643"; tr = from_origin(400000, 1900000, 10, 10)
    rng = np.random.default_rng(8)
    d = tmp_path / sid; d.mkdir()
    for k, base in (("vv", 0.08), ("vh", 0.02)):
        a = (base + base * 0.5 * rng.random((150, 150))).astype("float32")
        with rasterio.open(f"{d}/{k}.tif", 'w', driver='GTiff', height=150, width=150, count=1,
                           dtype="float32", crs=crs, transform=tr, nodata=-9999.0) as f:
            f.write(a, 1)
    inv = Transformer.from_crs(crs, "EPSG:4326", always_xy=True).transform
    return types.SimpleNamespace(
        id=sid, datetime=dt.datetime(2026, 9, day, tzinfo=dt.timezone.utc),
        properties={"platform": "sentinel-1c", "sat:relative_orbit": 63, "sar:instrument_mode": "IW"},
        geometry=mapping(shp_transform(inv, _b(390000, 1890000, 410000, 1910000))),
        assets={"vv": types.SimpleNamespace(href=f"{d}/vv.tif", extra_fields={}),
                "vh": types.SimpleNamespace(href=f"{d}/vh.tif", extra_fields={})})


def test_radar_block_read_equals_per_land_read_to_the_rvi_value(tmp_path):
    from shapely.geometry import box as _b, mapping
    from shapely.ops import transform as shp_transform, unary_union
    from pyproj import Transformer
    import processor, tile_reader
    item = _radar_scene(tmp_path)
    inv = Transformer.from_crs("EPSG:32643", "EPSG:4326", always_xy=True).transform
    parcels = [shp_transform(inv, _b(400100 + dx, 1899100 + dy, 400100 + dx + s, 1899100 + dy + s))
               for dx, dy, s in [(0, 0, 90), (250, 180, 60), (500, 350, 140)]]
    block = tile_reader.read_scene_block(item, ["vv", "vh"], unary_union(parcels), categorical=(), extra_pad_m=0.0)
    keys = ("rvi_value", "rvi_std", "effective_pixel_count", "valid_pixels", "total_pixels",
            "quality_score", "confidence_score", "evidence_confidence", "measurement_status", "scene_id")
    for i, g in enumerate(parcels):
        land = {"id": f"R{i}", "tenant_id": "T", "area_acres": 1.0, "boundary_geom": mapping(g)}
        rep_a, rep_b = {"geometry_fingerprint": "fp", "s1_rejects": []}, {"geometry_fingerprint": "fp", "s1_rejects": []}
        a = processor._process_s1(land, g, False, "high", 8100.0, rep_a, 8100.0, ledger={},
                                  s1_search=lambda: [("rtc", item)])
        b = processor._process_s1(land, g, False, "high", 8100.0, rep_b, 8100.0, ledger={},
                                  s1_search=lambda: [("rtc", item)], s1_block_for=lambda it: block)
        assert len(a) == len(b) == 1
        assert {k: a[0].get(k) for k in keys} == {k: b[0].get(k) for k in keys}


def test_radar_is_measured_even_when_optical_was_accepted(tmp_path, monkeypatch):
    """Two live lands accepted optical on 5 Sept and never had radar tried
    again; their newest data was 19 days old while radar passes existed."""
    from shapely.geometry import box as _b, mapping
    from shapely.ops import transform as shp_transform
    from pyproj import Transformer
    import processor
    monkeypatch.setattr(processor, "S1_MODE", "always")
    optical, parcels = _synthetic_scene(str(tmp_path))
    radar = _radar_scene(tmp_path, sid="S1_NEW", day=19)
    land = {"id": "L", "tenant_id": "T", "area_acres": 1.0, "boundary_geom": mapping(parcels[2]), "ndvi_thumbnail_url": "x"}
    rows, report = processor.process_land(land, scenes=[optical], history=[], s1_search=lambda: [("rtc", radar)])
    sources = sorted(r["observation_source"] for r in rows)
    assert sources == ["sentinel-1", "sentinel-2"]                 # both signals, same night
    radar_row = [r for r in rows if r["observation_source"] == "sentinel-1"][0]
    assert radar_row["scene_id"] == "S1_NEW" and radar_row.get("rvi_value") is not None
    # and once measured, the radar scene is skipped without reads next night
    reads = []
    monkeypatch.setattr(processor, "read_band", lambda *a, **k: reads.append(1))
    rows2, rep2 = processor.process_land(land, scenes=[], history=[], s1_search=lambda: [("rtc", radar)],
                                         ledger={"S1_NEW": [(processor.geometry_fingerprint(parcels[2]), "accepted")]})
    assert rows2 == [] and reads == [] and rep2["prior_accepted_in_window"] is True


# ---------------------------------------------------------------------------
# Production failure 2026-10-01 (land 8897e53d): rasterio returned an EMPTY CRS
# for a Sentinel-1 RTC asset -> `CRSError: Invalid projection: ""` -> the parcel
# got no radar reading. The STAC item still carried proj:epsg.
# ---------------------------------------------------------------------------
def _strip_crs(path):
    """Rewrite the GeoTIFF with the same pixels/transform/nodata but NO CRS tag
    (rasterio refuses `crs = None` in place)."""
    import rasterio
    with rasterio.open(path) as f:
        prof = f.profile.copy(); arr = f.read(1)
    prof.pop("crs", None)
    with rasterio.open(path, "w", **prof) as f:
        f.write(arr, 1)
    with rasterio.open(path) as f:
        assert not f.crs


def test_source_crs_prefers_raster_then_stac_projection(tmp_path):
    import rasterio
    from rasterio.crs import CRS as RCRS
    item = _radar_scene(tmp_path, sid="S1_CRS")
    with rasterio.open(item.assets["vv"].href) as src:
        assert ru.source_crs(src, item, "vv") == src.crs           # unchanged when present
    _strip_crs(item.assets["vv"].href)
    with rasterio.open(item.assets["vv"].href) as src:
        assert not src.crs                                          # the production condition
        with pytest.raises(ValueError):
            ru.source_crs(src, item, "vv")                          # nothing to fall back on -> explicit
        item.properties["proj:epsg"] = 32643
        assert ru.source_crs(src, item, "vv").to_epsg() == 32643    # item-level fallback
        item.assets["vv"].extra_fields["proj:epsg"] = 32643
        assert ru.source_crs(src, item, "vv").to_epsg() == 32643    # asset-level fallback
        del item.properties["proj:epsg"]; del item.assets["vv"].extra_fields["proj:epsg"]
        item.properties["proj:code"] = "EPSG:32643"
        assert ru.source_crs(src, item, "vv") == RCRS.from_epsg(32643)


def test_radar_read_survives_missing_raster_crs_and_matches_intact_read(tmp_path):
    """Same RVI from a CRS-less VV/VH pair (+ proj:epsg on the item) as from the
    intact files, through read_band and through the block reader."""
    from shapely.geometry import box as _b
    from shapely.ops import transform as shp_transform
    from pyproj import Transformer
    import processor, tile_reader
    intact = _radar_scene(tmp_path, sid="S1_OK")
    broken = _radar_scene(tmp_path, sid="S1_NOCRS")
    for k in ("vv", "vh"):
        _strip_crs(broken.assets[k].href)
    broken.properties["proj:epsg"] = 32643
    inv = Transformer.from_crs("EPSG:32643", "EPSG:4326", always_xy=True).transform
    g = shp_transform(inv, _b(400100, 1899100, 400400, 1899400))
    land = {"id": "L", "tenant_id": "T", "area_acres": 22.0}
    keys = ("rvi_value", "rvi_std", "cross_ratio_db", "effective_pixel_count", "coverage_weighted_purity")
    a = processor._process_s1(land, g, False, "high", 90000.0, {}, 90000.0, ledger={},
                              s1_search=lambda: [("rtc", intact)])
    b = processor._process_s1(land, g, False, "high", 90000.0, {}, 90000.0, ledger={},
                              s1_search=lambda: [("rtc", broken)])
    assert len(a) == len(b) == 1
    assert {k: a[0].get(k) for k in keys} == {k: b[0].get(k) for k in keys}
    block = tile_reader.read_scene_block(broken, ["vv", "vh"], g, categorical=())
    assert set(block.bands) == {"vv", "vh"}
    assert block.crs["vv"].to_epsg() == 32643
    c = processor._process_s1(land, g, False, "high", 90000.0, {}, 90000.0, ledger={},
                              s1_search=lambda: [("rtc", broken)], s1_block_for=lambda it: block)
    assert {k: c[0].get(k) for k in keys} == {k: a[0].get(k) for k in keys}
