"""
raster_utils.py - band I/O, reflectance scaling, geometry, masking.

The measurement path uses exact fractional pixel coverage on the 10 m
reference grid. Spectral values are converted to surface reflectance using
STAC raster:bands semantics: physical_value = raw * scale + offset. When
raster metadata are absent, Sentinel-2 processing-baseline >= 04.00 falls
back to the ESA BOA_ADD_OFFSET representation: (DN - 1000) / 10000.
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
    Return (geometry_wgs84, erosion_applied, raw_area_m2, measured_area_m2).

    Small fields are measured on the farmer polygon because exact fractional
    coverage already handles mixed boundary cells. Larger fields retain the
    existing -10 m erosion policy.
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


def buffered_field(geom):
    """Backwards-compatible alias returning the historical 3-tuple."""
    g, applied, raw, _ = measurement_field(geom)
    return g, applied, raw


# ---------------------------------------------------------------------------
# EXACT FRACTIONAL COVERAGE
# ---------------------------------------------------------------------------
def coverage_fractions(geom_proj, transform, shape):
    """Exact fraction of every raster cell covered by geom_proj."""
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
    """Hard upper bound on native cells in the geometry bounding box."""
    utm = utm_crs_for(geom)
    fwd = Transformer.from_crs("EPSG:4326", utm, always_xy=True).transform
    minx, miny, maxx, maxy = shp_transform(fwd, geom).bounds
    nx = int(np.ceil((maxx - minx) / cell_m)) + 1
    ny = int(np.ceil((maxy - miny) / cell_m)) + 1
    return max(nx * ny, 1)


# ---------------------------------------------------------------------------
# REFLECTANCE SCALING
# ---------------------------------------------------------------------------
def _baseline_number(item) -> float | None:
    try:
        value = str(item.properties.get("s2:processing_baseline", "")).strip()
        return float(value) if value else None
    except (TypeError, ValueError):
        return None


def band_scale_offset(item, band_key: str):
    """
    Return (scale, physical_offset) for the asset.

    STAC raster:bands `offset` is in the scaled physical unit and therefore
    MUST be applied after multiplication: physical = raw * scale + offset.

    If raster:bands metadata are absent, Sentinel-2 baseline >= 04.00 uses
    ESA's +1000 DN BOA shift, equivalent to physical_offset = -1000 * 1e-4.
    The old implementation added the physical offset to DN before scaling,
    which was dimensionally wrong and biased reflectance/NDVI.
    """
    default_scale = 1.0 / 10000.0
    scale = default_scale
    offset = 0.0
    metadata_found = False

    try:
        raster_bands = item.assets[band_key].extra_fields.get("raster:bands")
        if raster_bands:
            rb = raster_bands[0] or {}
            if rb.get("scale") is not None:
                scale = float(rb["scale"])
            if rb.get("offset") is not None:
                offset = float(rb["offset"])
            metadata_found = ("scale" in rb) or ("offset" in rb)
    except (AttributeError, KeyError, TypeError, ValueError):
        metadata_found = False

    if not metadata_found and offset == 0.0:
        baseline = _baseline_number(item)
        if baseline is not None and baseline >= 4.0:
            offset = -1000.0 * scale

    if not np.isfinite(scale) or scale <= 0.0:
        raise ValueError(f"Invalid STAC scale for {band_key}: {scale!r}")
    if not np.isfinite(offset):
        raise ValueError(f"Invalid STAC offset for {band_key}: {offset!r}")
    return scale, offset


def to_reflectance(data: np.ndarray, item, band_key: str) -> np.ndarray:
    """Convert DN to surface reflectance with physically valid masking."""
    scale, offset = band_scale_offset(item, band_key)
    if np.ma.isMaskedArray(data):
        arr = np.ma.filled(data.astype("float32"), np.nan)
        arr[np.ma.getmaskarray(data)] = np.nan
    else:
        arr = np.asarray(data, dtype="float32")

    # STAC semantics: scale first, then add the physical offset.
    out = arr * scale + offset
    out[(out < 0.0) | (out > REFLECTANCE_MAX)] = np.nan
    return out.astype("float32", copy=False)


# ---------------------------------------------------------------------------
# BAND READ
# ---------------------------------------------------------------------------
def read_band(item, band_key: str, geometry, reference=None, categorical=False):
    """Every remote Sentinel-2 raster read in the pipeline goes through here -
    processor, parcel context, water layers and the block-path fallback. So
    this is the one place where counting is COMPLETE by construction: the
    canary's raster_reads and throttling counters cannot miss a stage.
    (release-audit finding: counters in individual stages undercounted.)"""
    from resource_budget import COUNTERS
    try:
        out = _read_band_impl(item, band_key, geometry, reference=reference,
                              categorical=categorical)
    except Exception as exc:
        COUNTERS.classify_read_error(exc)
        raise
    COUNTERS.bump("raster_reads")
    return out


def _read_band_impl(item, band_key: str, geometry, reference=None, categorical=False):
    """Read a band clipped to geometry and optionally reproject to reference."""
    asset = item.assets[band_key]

    with rasterio.open(asset.href) as src:
        geom_proj = reproject_geometry(geometry, src.crs)
        nodata = src.nodata if src.nodata is not None else 0

        pad_m = 0.0
        if categorical and CLOUD_DILATION_PX > 0:
            pad_m = CLOUD_DILATION_PX * 10.0 + max(abs(src.res[0]), 10.0)
        clip_geom = geom_proj.buffer(pad_m) if pad_m else geom_proj

        data, transform = rio_mask(
            src, [mapping(clip_geom)], crop=True, filled=False,
            all_touched=True, nodata=nodata,
        )
        data = data[0] if data.ndim == 3 else data

        if reference is None:
            cov, cov_method = coverage_fractions(geom_proj, transform, data.shape)
            if cov is None:
                cov = (~np.ma.getmaskarray(data)).astype("float32")
                logger.warning(f"coverage fallback ({cov_method}) for {band_key}")
        else:
            cov = None

        inside = (cov > 0) if cov is not None else ~np.ma.getmaskarray(data)
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
            reproject(
                source=arr, destination=dst,
                src_transform=transform, src_crs=src.crs,
                dst_transform=ref_transform, dst_crs=ref_crs,
                src_nodata=0, dst_nodata=0,
                resampling=Resampling.nearest,
            )
        else:
            dst = np.full(ref_shape, np.nan, dtype="float32")
            reproject(
                source=arr, destination=dst,
                src_transform=transform, src_crs=src.crs,
                dst_transform=ref_transform, dst_crs=ref_crs,
                src_nodata=np.nan, dst_nodata=np.nan,
                resampling=Resampling.bilinear,
            )
            dst[~ref_footprint] = np.nan
        return dst, ref_transform, ref_crs, ref_coverage


# ---------------------------------------------------------------------------
# MASKING
# ---------------------------------------------------------------------------
def dilate_scl(scl: np.ndarray, native_res_m: float = 20.0) -> np.ndarray:
    """Dilate SCL cloud/shadow classes on the native grid."""
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
    """Return area-weighted SCL masks over the measured field."""
    in_field = scl != 0
    if coverage is not None:
        w = np.where(in_field, coverage, 0.0).astype("float64")
    else:
        w = in_field.astype("float64")
    in_field = w > 0
    n = int(np.count_nonzero(in_field))
    epc_total = float(w.sum())

    cloud = np.isin(scl, SCL_CLOUD) & in_field
    shadow = np.isin(scl, SCL_SHADOW) & in_field & ~cloud
    dark = np.isin(scl, SCL_DARK) & in_field
    water = np.isin(scl, SCL_WATER) & in_field & ~cloud & ~shadow
    saturated = np.isin(scl, SCL_SATURATED) & in_field & ~cloud & ~shadow
    snow = np.isin(scl, SCL_SNOW) & in_field & ~cloud & ~shadow
    crop = np.isin(scl, SCL_CROP_SURFACE) & in_field & ~cloud & ~shadow

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
    """Set every non-crop-surface pixel to NaN across spectral bands."""
    keep = masks["crop"]
    return {
        k: (v if k == "SCL" else np.where(keep, v, np.nan))
        for k, v in bands.items()
    }


# ---------------------------------------------------------------------------
# GEOMETRY RESOLUTION WITH HONEST CONFIDENCE
# ---------------------------------------------------------------------------
CENTROID_BUFFER_DEG = 0.00036


def resolve_geometry(land: dict):
    """Return (geometry, confidence) from surveyed/legacy land geometry."""
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
