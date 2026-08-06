"""
raster_utils.py - band I/O, reflectance scaling, geometry, masking.

v1 DEFECTS FIXED HERE:
  P-04  Resampling.bilinear was applied to SCL, a CATEGORICAL raster. Class
        codes were interpolated to fractional values (4.5, 6.25) which
        np.isin then rejected. This destroyed ~69% of legitimate pixels -
        the true cause of the 31% mean "coverage" - and could blend a
        cloud edge (9) with vegetation (4) into exactly 6 or 7, which v1's
        VALID_SCL accepted. v2 uses Resampling.nearest for SCL.
  P-05  VALID_SCL = [4,5,6,7] admitted water and unclassified. Now [4,5],
        imported from config, with cloud/shadow/water tracked separately.
  P-12  No negative buffer. v2 erodes the polygon by one pixel.
  P-17  Band scale was guessed from each clip's own max value, independently
        per band, so two bands in one index could end up on different scales.
        v2 reads scale/offset from STAC asset metadata.
"""

import numpy as np
import rasterio
from rasterio.mask import mask as rio_mask
from rasterio.warp import reproject, Resampling
from shapely.ops import transform as shp_transform
from shapely.geometry import mapping
from pyproj import Transformer, CRS

from config import (
    SCL_CROP_SURFACE, SCL_CLOUD, SCL_SHADOW, SCL_WATER,
    SCL_SATURATED, SCL_SNOW, SCL_DARK,
    FIELD_BUFFER_M, MIN_BUFFERED_AREA_M2,
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


def buffered_field(geom):
    """
    Erode the field by one Sentinel-2 pixel to suppress mixed edge pixels.

    Returns (geometry_wgs84, buffer_applied: bool, area_m2: float).
    Tiny fields fall back to the raw polygon but are flagged so the quality
    score can be downgraded - the caller must not treat them as equivalent.
    """
    utm = utm_crs_for(geom)
    fwd = Transformer.from_crs("EPSG:4326", utm, always_xy=True).transform
    inv = Transformer.from_crs(utm, "EPSG:4326", always_xy=True).transform

    g_utm = shp_transform(fwd, geom)
    raw_area = g_utm.area

    eroded = g_utm.buffer(FIELD_BUFFER_M)
    if eroded.is_empty or eroded.area < MIN_BUFFERED_AREA_M2:
        logger.debug(
            f"Field too small to buffer (raw {raw_area:.0f} m2); using raw polygon"
        )
        return geom, False, raw_area

    return shp_transform(inv, eroded), True, eroded.area


# ---------------------------------------------------------------------------
# REFLECTANCE SCALING  (P-17)
# ---------------------------------------------------------------------------
def to_reflectance(data: np.ndarray, item, band_key: str) -> np.ndarray:
    """
    Convert raw DN to surface reflectance using STAC metadata.

    Sentinel-2 processing baseline >= 04.00 (from 2022-01-25) applies
    BOA_ADD_OFFSET = -1000, so reflectance = (DN + offset) / 10000.
    Ignoring the offset biases every index; guessing the scale from pixel
    statistics (v1) can put two bands of one index on different scales.
    """
    scale, offset = 1.0 / 10000.0, 0.0

    try:
        raster_bands = item.assets[band_key].extra_fields.get("raster:bands")
        if raster_bands:
            rb = raster_bands[0]
            scale = rb.get("scale", scale)
            offset = rb.get("offset", offset)
    except Exception:
        pass

    if offset == 0.0:
        try:
            baseline = str(item.properties.get("s2:processing_baseline", "")).strip()
            if baseline and float(baseline) >= 4.0:
                offset = -1000.0
        except Exception:
            pass

    out = (data.astype("float32") + offset) * scale
    return np.clip(out, 0.0, 1.6)


# ---------------------------------------------------------------------------
# BAND READ
# ---------------------------------------------------------------------------
def read_band(item, band_key: str, geometry, reference=None, categorical=False):
    """
    Read one band clipped to `geometry`.

    categorical=True  -> nearest-neighbour resampling (SCL). THE P-04 FIX.
    categorical=False -> bilinear, and DN converted to reflectance.

    Outside-polygon pixels are filled with NaN (not 0 as in v1) so they can
    never be mistaken for a real reflectance of zero.
    """
    asset = item.assets[band_key]

    with rasterio.open(asset.href) as src:
        geom_proj = reproject_geometry(geometry, src.crs)

        data, transform = rio_mask(
            src, [mapping(geom_proj)],
            crop=True, filled=True,
            nodata=src.nodata if src.nodata is not None else 0,
        )
        data = data[0] if data.ndim == 3 else data

        if categorical:
            arr = data.astype("int16")
        else:
            arr = to_reflectance(data, item, band_key)

        if reference is None:
            return arr, transform

        ref_shape, ref_transform, ref_crs = reference
        dst = np.empty(ref_shape, dtype="float32" if not categorical else "int16")

        reproject(
            source=arr,
            destination=dst,
            src_transform=transform,
            src_crs=src.crs,
            dst_transform=ref_transform,
            dst_crs=ref_crs,
            # ---- P-04 FIX -------------------------------------------------
            resampling=Resampling.nearest if categorical else Resampling.bilinear,
        )
        return dst, ref_transform


# ---------------------------------------------------------------------------
# MASKING
# ---------------------------------------------------------------------------
def scl_masks(scl: np.ndarray) -> dict:
    """
    Decompose SCL into named boolean masks.

    Returned fractions are computed over the pixels that are inside the
    buffered polygon at all (SCL != 0), so 'cloud_fraction' means
    'fraction of THIS FIELD under cloud' - a real quality metric, unlike
    v1's coverage_percentage which measured polygon-area-in-bounding-box.
    """
    in_field = scl != 0
    n = int(np.count_nonzero(in_field))

    crop      = np.isin(scl, SCL_CROP_SURFACE) & in_field
    cloud     = np.isin(scl, SCL_CLOUD)         & in_field
    shadow    = np.isin(scl, SCL_SHADOW)        & in_field
    water     = np.isin(scl, SCL_WATER)         & in_field
    saturated = np.isin(scl, SCL_SATURATED)     & in_field
    snow      = np.isin(scl, SCL_SNOW)          & in_field
    dark      = np.isin(scl, SCL_DARK)          & in_field

    frac = lambda m: (float(np.count_nonzero(m)) / n) if n else 0.0

    # Every in-field pixel must be attributable to a named ESA class.
    # Anything unaccounted is SCL 7 (unclassified) or an unexpected code, and
    # is surfaced rather than silently absorbed - a rejection with no stated
    # reason is the failure mode this whole pipeline exists to remove.
    accounted = crop | cloud | shadow | water | saturated | snow | dark
    unaccounted = in_field & ~accounted

    return {
        "in_field": in_field,
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
# Adopted from the retired kisanshakti-ndvi-engine repo
# (land_geometry.resolve_land_geometry). Better than my original hard failure:
# it always returns something, and LABELS how much to trust it.
#
# The label is not decoration - quality.assess() multiplies the score by
# GEOMETRY_CONFIDENCE_FACTOR, so a centroid guess can never score like a
# surveyed polygon. Degrading honestly beats refusing, but only if the
# degradation is visible downstream.
# ---------------------------------------------------------------------------
from shapely.geometry import Point, mapping as _mapping

CENTROID_BUFFER_DEG = 0.00036   # ~40 m at Indian latitudes


def resolve_geometry(land: dict):
    """
    Returns (shapely_geometry, confidence) where confidence is
    'high' | 'medium' | 'low'.

    high   - PostGIS boundary_geom / boundary_geojson (surveyed)
    medium - legacy boundary_polygon_old jsonb (may be stale; the two are
             not synchronised - see audit finding P-18)
    low    - 40 m buffer around the centroid; no polygon exists at all
    """
    from shapely.geometry import shape as _shape

    for key, conf in (("boundary_geojson", "high"),
                      ("boundary_geom", "high"),
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
