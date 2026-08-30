"""
raster_utils.py - band I/O, reflectance scaling, geometry, masking.

v3 CHANGE (smallholder evidence audit): EXACT FRACTIONAL COVERAGE
--------------------------------------------------------------------
read_band() now returns, alongside the array, the exact fraction of each
10 m cell that lies inside the measurement polygon. Whole-pixel counting
is gone from the measurement path: a cell 12 % inside the field is worth
0.12 of a pixel, not 1. This removes the boundary over-count that made
all_touched sample up to ~181 % of a sub-0.5-acre field, and it makes the
support metric (EPC) area-true by construction.

all_touched is NOT removed - it is demoted. It now decides only WHICH
cells are candidates (every cell the polygon touches); the coverage
fraction decides what each one is worth. Selecting with centre-sampling
instead would silently discard real crop area on narrow strips.

Fixed -10 m erosion is likewise demoted to large fields only
(ADAPTIVE_EROSION_MIN_AREA_M2): on a square 10-guntha field it removes
~86 % of the area, and with coverage weighting it no longer buys anything.

v2.2 CHANGES (forensic audit 2026-08-29, finding F-1 / F-8 / F-10)
--------------------------------------------------------------------
F-1  The field FOOTPRINT was defined as `SCL != 0`. SCL is a 20 m band; its
     clip overhangs the 10 m reference grid, so reference cells OUTSIDE the
     polygon received a real SCL class while B04/B08 held the nodata fill (0),
     which to_reflectance turned into 0.0 and NDVI turned into exactly 0.0.
     Live evidence: 16/16 optical rows had more pixels than the field area
     allows and 13/16 had ndvi_spatial_min == 0.0. Means were biased low by
     25-60 %.
     FIX: read_band now returns the rasterio *mask* of the reference band
     (True = inside polygon at 10 m). scl_masks() intersects SCL with it.
     Nothing outside the surveyed boundary can be counted any more.

F-8  Fill outside the polygon is now NaN (masked read), so bilinear
     resampling of the 20 m bands (B05, B11) cannot blend zeros into edge
     pixels. reproject() propagates NaN; the shared finite mask in indices.py
     drops those cells.

F-10 Negative surface reflectance (deep shadow / water after offset) is now
     NaN, not 0.0. A pixel with no physical reflectance must not produce a
     "valid" index value.

CLOUD-EDGE DILATION (research-backed, new)
     Sen2Cor's SCL under-detects cloud and shadow edges; production
     time-series work routinely dilates the cloud/shadow mask by 1-6 pixels
     (CMIX 2022; MDPI RS 14:4221 uses 120 m). We dilate cloud (8,9,10) and
     cloud shadow (3) by CLOUD_DILATION_PX on the 10 m grid. Dilated pixels
     are treated as cloud, so a field touched by a cloud edge is scored and
     gated honestly rather than measured through haze.

Earlier v1 fixes retained: SCL nearest resampling (P-04), SCL_CROP_SURFACE
[4,5] from config (P-05), -10 m erosion (P-12), scale/offset from STAC
metadata with baseline fallback (P-17).
"""

import numpy as np
import shapely
import rasterio
from rasterio.mask import mask as rio_mask
from rasterio.warp import reproject, Resampling
from scipy.ndimage import binary_dilation
from shapely.ops import transform as shp_transform
from shapely.geometry import mapping, Point
from pyproj import Transformer, CRS

from config import (
    SCL_CROP_SURFACE, SCL_CLOUD, SCL_SHADOW, SCL_WATER,
    SCL_SATURATED, SCL_SNOW, SCL_DARK, SCL_CLOUD_SHADOW,
    FIELD_BUFFER_M, MIN_BUFFERED_AREA_M2,
    CLOUD_DILATION_PX, REFLECTANCE_MAX,
    MAX_COVERAGE_CELLS, MIN_CELL_COVERAGE, ADAPTIVE_EROSION_MIN_AREA_M2,
)
from logger import logger


# ---------------------------------------------------------------------------
# GEOMETRY
# ---------------------------------------------------------------------------
def reproject_geometry(geom, dst_crs):
    t = Transformer.from_crs("EPSG:4326", dst_crs, always_xy=True)
    return shp_transform(t.transform, geom)


def utm_crs_for(geom):
    """Local UTM zone so buffering happens in metres, not degrees."""
    lon = geom.centroid.x
    lat = geom.centroid.y
    zone = int((lon + 180) // 6) + 1
    epsg = 32600 + zone if lat >= 0 else 32700 + zone
    return CRS.from_epsg(epsg)


def measurement_field(geom):
    """
    Decide the polygon that will actually be measured.

    Returns (geometry_wgs84, buffer_applied, raw_area_m2, measured_area_m2).

    ADAPTIVE (v3). A fixed -10 m erosion is only sane for large fields: on a
    square 10-guntha parcel (1012 m2, ~31.8 m a side) it leaves ~139 m2, i.e.
    it throws away ~86 % of the farm. Since v3 weights every cell by its
    exact coverage, mixed edge pixels are already handled by the weights and
    erosion buys nothing on small fields. So:

        area >= ADAPTIVE_EROSION_MIN_AREA_M2 -> erode FIELD_BUFFER_M
        area <  ADAPTIVE_EROSION_MIN_AREA_M2 -> measure the farmer's polygon

    The measured polygon is recorded per row (metadata.evidence) because the
    two regimes are different physical definitions of "the field" and must
    not be compared blindly across a trend.
    """
    utm = utm_crs_for(geom)
    fwd = Transformer.from_crs("EPSG:4326", utm, always_xy=True).transform
    inv = Transformer.from_crs(utm, "EPSG:4326", always_xy=True).transform

    g_utm = shp_transform(fwd, geom)
    raw_area = g_utm.area

    if raw_area < ADAPTIVE_EROSION_MIN_AREA_M2:
        return geom, False, raw_area, raw_area

    eroded = g_utm.buffer(FIELD_BUFFER_M)
    if eroded.is_empty or eroded.area < MIN_BUFFERED_AREA_M2:
        return geom, False, raw_area, raw_area

    return shp_transform(inv, eroded), True, raw_area, eroded.area


# Backwards-compatible alias (old name, 3-tuple) for any external caller.
def buffered_field(geom):
    g, applied, raw, _ = measurement_field(geom)
    return g, applied, raw


# ---------------------------------------------------------------------------
# EXACT FRACTIONAL COVERAGE
# ---------------------------------------------------------------------------
def coverage_fractions(geom_proj, transform, shape):
    """
    Exact fraction of each raster cell covered by `geom_proj`.

    geom_proj : polygon ALREADY in the raster CRS (metres).
    Returns (coverage float32 [0,1] of `shape`, method: str).

    Computed analytically with shapely intersections - no sampling, no
    approximation - so the identity

        coverage.sum() * cell_area == polygon area inside the window

    holds to floating-point precision and is asserted by the caller. Falls
    back to binary coverage only for rotated grids or windows larger than
    MAX_COVERAGE_CELLS, and reports which happened.
    """
    h, w = shape
    a, b, _c, d, e, _f = transform.a, transform.b, transform.c, transform.d, transform.e, transform.f
    if b != 0 or d != 0:
        return None, "binary_rotated_grid"
    if h * w > MAX_COVERAGE_CELLS:
        return None, "binary_window_too_large"

    cols = np.arange(w)
    rows = np.arange(h)
    x0 = transform.c + cols * a
    x1 = x0 + a
    y0 = transform.f + rows * e
    y1 = y0 + e

    X0, Y0 = np.meshgrid(x0, y0)
    X1, Y1 = np.meshgrid(x1, y1)
    xmin = np.minimum(X0, X1).ravel()
    xmax = np.maximum(X0, X1).ravel()
    ymin = np.minimum(Y0, Y1).ravel()
    ymax = np.maximum(Y0, Y1).ravel()

    cov = np.zeros(h * w, dtype="float64")
    gminx, gminy, gmaxx, gmaxy = geom_proj.bounds
    cand = np.where((xmax > gminx) & (xmin < gmaxx) &
                    (ymax > gminy) & (ymin < gmaxy))[0]
    if cand.size:
        cells = shapely.box(xmin[cand], ymin[cand], xmax[cand], ymax[cand])
        inter = shapely.intersection(cells, geom_proj)
        cov[cand] = shapely.area(inter) / abs(a * e)

    cov = np.clip(cov, 0.0, 1.0).reshape(h, w).astype("float32")
    cov[cov < MIN_CELL_COVERAGE] = 0.0
    return cov, "exact_shapely"


def bbox_cells(geom, cell_m: float = 10.0) -> int:
    """
    Hard upper bound on the number of `cell_m` grid cells any clip of
    `geom` can contain, in either sampling mode: every cell (centre-sampled
    OR all_touched) lies inside the UTM bounding box grown by one cell on
    each axis. Used by the pixel-plausibility gate for narrow strips, where
    all_touched legitimately touches ~2x the area-based count.
    """
    utm = utm_crs_for(geom)
    fwd = Transformer.from_crs("EPSG:4326", utm, always_xy=True).transform
    minx, miny, maxx, maxy = shp_transform(fwd, geom).bounds
    nx = int(np.ceil((maxx - minx) / cell_m)) + 1
    ny = int(np.ceil((maxy - miny) / cell_m)) + 1
    return max(nx * ny, 1)


# ---------------------------------------------------------------------------
# REFLECTANCE SCALING  (P-17)
# ---------------------------------------------------------------------------
def band_scale_offset(item, band_key: str):
    """
    (scale, offset) for a Sentinel-2 L2A asset.

    Planetary Computer does NOT harmonise the BOA_ADD_OFFSET introduced with
    processing baseline 04.00 (2022-01-25); DN carry a +1000 shift. Prefer
    STAC raster:bands metadata when present; otherwise fall back to the
    baseline rule. Returned offset is what is ADDED before scaling.
    """
    scale, offset = 1.0 / 10000.0, 0.0
    try:
        raster_bands = item.assets[band_key].extra_fields.get("raster:bands")
        if raster_bands:
            rb = raster_bands[0]
            scale = float(rb.get("scale", scale))
            offset = float(rb.get("offset", offset))
    except Exception:
        pass

    if offset == 0.0:
        try:
            baseline = str(item.properties.get("s2:processing_baseline", "")).strip()
            if baseline and float(baseline) >= 4.0:
                offset = -1000.0
        except Exception:
            pass
    return scale, offset


def to_reflectance(data: np.ndarray, item, band_key: str) -> np.ndarray:
    """
    DN -> surface reflectance. Masked / nodata cells become NaN.
    Physically impossible values (< 0 after offset, > REFLECTANCE_MAX)
    become NaN rather than being clipped into a plausible-looking number.
    """
    scale, offset = band_scale_offset(item, band_key)
    arr = np.ma.filled(data.astype("float32"), np.nan)
    if np.ma.isMaskedArray(data):
        arr[np.ma.getmaskarray(data)] = np.nan
    out = (arr + offset) * scale
    out[(out < 0.0) | (out > REFLECTANCE_MAX)] = np.nan
    return out


# ---------------------------------------------------------------------------
# BAND READ
# ---------------------------------------------------------------------------
def read_band(item, band_key: str, geometry, reference=None, categorical=False):
    """
    Read one band clipped to `geometry`.

    Returns (array, transform, crs, coverage).
      coverage : float32 [0,1] on THIS band's grid - the exact fraction of
                 each cell inside the measurement polygon. For the reference
                 band it is computed analytically; reprojected bands inherit
                 the reference coverage. coverage > 0 replaces the old
                 boolean footprint everywhere.

    categorical=True  -> nearest-neighbour resampling, int16, fill 0 (SCL).
    categorical=False -> bilinear, float32, fill NaN, DN -> reflectance.
    """
    asset = item.assets[band_key]

    with rasterio.open(asset.href) as src:
        geom_proj = reproject_geometry(geometry, src.crs)
        nodata = src.nodata if src.nodata is not None else 0

        # SCL is read over a PADDED window so that a cloud / shadow lying just
        # outside the boundary is still dilated into the field. The pad is
        # the dilation distance plus one native cell. Pixel-native (m) CRS
        # is guaranteed for Sentinel-2 (UTM).
        pad_m = 0.0
        if categorical and CLOUD_DILATION_PX > 0:
            pad_m = CLOUD_DILATION_PX * 10.0 + max(abs(src.res[0]), 10.0)
        clip_geom = geom_proj.buffer(pad_m) if pad_m else geom_proj

        # v3: all_touched=True is now the SELECTION rule - every cell the
        # polygon touches is a candidate. The exact coverage fraction, not
        # the on/off mask, decides what each candidate is worth. Centre-only
        # selection would drop real crop area on strips narrower than ~2 px.
        data, transform = rio_mask(src, [mapping(clip_geom)], crop=True,
                                   filled=False, all_touched=True,
                                   nodata=nodata)
        data = data[0] if data.ndim == 3 else data

        if reference is None:
            cov, cov_method = coverage_fractions(geom_proj, transform, data.shape)
            if cov is None:
                cov = (~np.ma.getmaskarray(data)).astype("float32")
                logger.warning(f"coverage fallback ({cov_method}) for {band_key}")
        else:
            cov = None          # inherited from the reference grid below
            cov_method = "inherited"

        inside = (cov > 0) if cov is not None else ~np.ma.getmaskarray(data)
        # Genuine nodata INSIDE the polygon contributes nothing.
        inside &= (np.ma.getdata(data) != nodata)
        if cov is not None:
            cov = np.where(inside, cov, 0.0).astype("float32")

        if categorical:
            arr = np.ma.filled(data, 0).astype("int16")
            arr[~inside] = 0
            if CLOUD_DILATION_PX > 0:
                arr = dilate_scl(arr, native_res_m=abs(src.res[0]))
        else:
            arr = to_reflectance(data, item, band_key)
            arr[~inside] = np.nan

        if reference is None:
            return arr, transform, src.crs, cov

        ref_shape, ref_transform, ref_crs, ref_coverage = reference
        ref_footprint = ref_coverage > 0
        if ref_crs is None:
            raise ValueError(
                f"reference grid for band {band_key} has no CRS; "
                f"read_band must return (array, transform, crs, footprint)"
            )

        if categorical:
            dst = np.zeros(ref_shape, dtype="int16")
            reproject(source=arr, destination=dst,
                      src_transform=transform, src_crs=src.crs,
                      dst_transform=ref_transform, dst_crs=ref_crs,
                      src_nodata=0, dst_nodata=0,
                      resampling=Resampling.nearest)
        else:
            dst = np.full(ref_shape, np.nan, dtype="float32")
            reproject(source=arr, destination=dst,
                      src_transform=transform, src_crs=src.crs,
                      dst_transform=ref_transform, dst_crs=ref_crs,
                      src_nodata=np.nan, dst_nodata=np.nan,
                      resampling=Resampling.bilinear)
            dst[~ref_footprint] = np.nan
        return dst, ref_transform, ref_crs, ref_coverage


# ---------------------------------------------------------------------------
# MASKING
# ---------------------------------------------------------------------------
def dilate_scl(scl: np.ndarray, native_res_m: float = 20.0) -> np.ndarray:
    """
    Grow SCL cloud (8/9/10) and cloud-shadow (3) classes by CLOUD_DILATION_PX
    ten-metre pixels on the band's NATIVE grid. Grown cells are re-labelled
    9 (cloud) or 3 (shadow) only where they overwrite a non-cloud, non-nodata
    class, so class accounting stays exact. Applied BEFORE reprojection so the
    padded clip window lets outside clouds reach into the field.
    """
    if CLOUD_DILATION_PX <= 0:
        return scl
    iters = max(1, int(np.ceil(CLOUD_DILATION_PX * 10.0 / max(native_res_m, 1.0))))
    out = scl.copy()
    valid = scl != 0
    cloud = np.isin(scl, SCL_CLOUD)
    shadow = np.isin(scl, SCL_CLOUD_SHADOW)
    if cloud.any():
        grown = binary_dilation(cloud, iterations=iters) & valid & ~cloud
        out[grown] = 9
    if shadow.any():
        grown = binary_dilation(shadow, iterations=iters) & valid & ~np.isin(out, SCL_CLOUD) & ~shadow
        out[grown] = 3
    return out


def scl_masks(scl: np.ndarray, coverage: np.ndarray = None) -> dict:
    """
    Decompose SCL into named masks over the field, with AREA-WEIGHTED
    fractions.

    coverage : per-cell coverage fraction from read_band(). Every reported
               fraction is a share of FIELD AREA (sum of coverage), not a
               share of touched cells - so a cloud clipping one corner of a
               boundary cell no longer counts as a whole cloudy pixel.

    Cloud (8,9,10) and cloud shadow (3) arrive already dilated by
    CLOUD_DILATION_PX (see dilate_scl); the ring is counted as that class.
    """
    in_field = scl != 0
    if coverage is not None:
        w = np.where(in_field, coverage, 0.0).astype("float64")
    else:
        w = in_field.astype("float64")
    in_field = w > 0
    n = int(np.count_nonzero(in_field))
    epc_total = float(w.sum())

    # Dilation already applied on the native grid by read_band()/dilate_scl().
    cloud     = np.isin(scl, SCL_CLOUD)     & in_field
    shadow    = np.isin(scl, SCL_SHADOW)    & in_field & ~cloud
    dark      = np.isin(scl, SCL_DARK)      & in_field
    water     = np.isin(scl, SCL_WATER)     & in_field & ~cloud & ~shadow
    saturated = np.isin(scl, SCL_SATURATED) & in_field & ~cloud & ~shadow
    snow      = np.isin(scl, SCL_SNOW)      & in_field & ~cloud & ~shadow
    crop      = np.isin(scl, SCL_CROP_SURFACE) & in_field & ~cloud & ~shadow

    # AREA-weighted fraction (share of EPC), not cell-count fraction.
    frac = lambda m: (float(w[m].sum()) / epc_total) if epc_total > 0 else 0.0

    accounted = crop | cloud | shadow | water | saturated | snow | dark
    unaccounted = in_field & ~accounted

    return {
        "in_field": in_field,
        "coverage": w,
        "epc_total": epc_total,
        "epc_crop": float(w[crop].sum()),
        "crop": crop,
        "cloud": cloud,
        "shadow": shadow,
        "water": water,
        "saturated": saturated,
        "snow": snow,
        "dark": dark,
        "n_field_pixels": n,
        "n_crop_pixels": int(np.count_nonzero(crop)),
        "cloud_fraction": frac(cloud),
        "shadow_fraction": frac(shadow),
        "water_fraction": frac(water),
        "saturated_fraction": frac(saturated),
        "snow_fraction": frac(snow),
        "dark_fraction": frac(dark),
        "unaccounted_fraction": frac(unaccounted),
        "crop_fraction": frac(crop),
        "cloud_dilation_px": CLOUD_DILATION_PX,
    }


def apply_crop_mask(bands: dict, masks: dict) -> dict:
    """Set every non-crop-surface pixel to NaN across all spectral bands."""
    keep = masks["crop"]
    return {
        k: (v if k == "SCL" else np.where(keep, v, np.nan))
        for k, v in bands.items()
    }


# ---------------------------------------------------------------------------
# GEOMETRY RESOLUTION WITH HONEST CONFIDENCE
# ---------------------------------------------------------------------------
CENTROID_BUFFER_DEG = 0.00036   # ~40 m at Indian latitudes


def resolve_geometry(land: dict):
    """
    Returns (shapely_geometry, confidence) where confidence is
    'high' | 'medium' | 'low'.

    high   - PostGIS boundary_geom (surveyed; PostgREST serialises as GeoJSON)
    medium - legacy boundary_polygon_old jsonb (not synchronised with the above)
    low    - 40 m buffer around the centroid; no polygon exists at all
    """
    from shapely.geometry import shape as _shape

    for key, conf in (("boundary_geom", "high"),
                      ("boundary_geojson", "high"),
                      ("boundary", "high"),
                      ("boundary_polygon_old", "medium")):
        raw = land.get(key)
        if not raw:
            continue
        try:
            g = _shape(raw)
            if not g.is_valid:
                g = g.buffer(0)
            if not g.is_empty:
                return g, conf
        except Exception:
            continue

    lat, lon = land.get("center_lat"), land.get("center_lon")
    if lat is not None and lon is not None:
        logger.warning(
            f"Land {land.get('id')}: no polygon, using 40 m centroid buffer "
            f"(geometry_confidence=low)"
        )
        return Point(float(lon), float(lat)).buffer(CENTROID_BUFFER_DEG), "low"

    raise ValueError(f"Land {land.get('id')} has no usable geometry")
