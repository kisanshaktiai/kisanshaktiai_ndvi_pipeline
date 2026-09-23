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
    assert ev["measurement_status"] in ("OBSERVED_STRONG", "OBSERVED_LIMITED")
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
    monkeypatch.setattr(main, "log_steps_batch", lambda payloads: len(payloads))
    monkeypatch.setattr(main, "persist_observed_intelligence_batch",
                        lambda rows: ({r["land_id"]: 1 for r in rows}, {}))
    water = []
    monkeypatch.setattr(main, "process_land_water_layers",
                        lambda land, scenes=None, lookback_days=20, scene_bands=None, only_scene_ids=None:
                        water.append(only_scene_ids) or 0)
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
