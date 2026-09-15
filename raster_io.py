"""
raster_io.py - render the field NDVI raster and upload it to Supabase.

WHY THIS EXISTS
---------------
v1 wrote per-land thumbnails. The v2 rewrite moved to one-row-per-
acquisition and never ported the image path, so from v2 onward the
pipeline produced no imagery at all: ndvi_data.image_url is NULL on
every v2/v3 row, lands.ndvi_thumbnail_url is NULL on all 30 lands, and
the newest object in ndvi-thumbnails dates from 2026-06-10. The farmer
app therefore falls back to painting the boundary a single flat colour
(NDVIMapView renderMode 'zonal'), which throws away the within-field
variability that makes 10 m data worth having.

TWO HARD CONSTRAINTS, both read out of the app rather than assumed:

1. GEOGRAPHIC EXTENT. NDVIMapView adds the PNG as a MapLibre `image`
   source with
       coordinates: [[w,n],[e,n],[e,s],[w,s]]   where [[w,s],[e,n]] =
       computeBounds(boundary)
   i.e. the image is stretched north-up across the land polygon's
   WGS84 bounding box. The NDVI array lives on a UTM grid whose window
   is NOT that bbox. Handing over the native array would place the
   heatmap crooked and offset over the farmer's field - worse than
   showing nothing. So the array is warped to EPSG:4326 on a regular
   lat/lon grid spanning exactly those bounds.

2. COLOUR RAMP. The legend is drawn by the app from
   NDVI_COLOR_STOPS in src/lib/ndviScience.ts. NDVI_STOPS below is a
   verbatim copy of it, and the interpolation matches ndviToColor().
   If the two drift, the legend lies about the picture. Any change to
   one must be mirrored in the other.

Resampling is NEAREST throughout. The app's own rule is "never
interpolate between dates"; the same applies within a date - a farmer
must not be shown a smoothed value that no pixel actually measured.

Cells outside the measurement polygon, and cells masked as cloud /
shadow / non-crop, are written fully TRANSPARENT rather than given a
colour. The image then shows exactly the area the statistics were
computed over, and nothing else.
"""

from __future__ import annotations

import io
from typing import Optional, Tuple

import numpy as np
import rasterio
from rasterio.transform import from_bounds
from rasterio.warp import reproject, Resampling
from PIL import Image

from config import (
    NDVI_IMAGE_MAX_PX, NDVI_IMAGE_MIN_PX, NDVI_IMAGE_BUCKET,
    NDVI_IMAGE_SIGNED_URL_TTL,
)
from logger import logger


# Verbatim from src/lib/ndviScience.ts NDVI_COLOR_STOPS - keep in sync.
NDVI_STOPS: list[tuple[float, str]] = [
    (-0.20, "#7C3F1C"),   # bare soil / built-up
    ( 0.00, "#B25C2C"),
    ( 0.10, "#D9B26E"),
    ( 0.20, "#E8C170"),   # critical -> poor
    ( 0.35, "#FFD166"),   # poor -> moderate
    ( 0.50, "#C7E27A"),   # moderate -> healthy
    ( 0.65, "#5DBB63"),   # healthy -> excellent
    ( 0.80, "#2E8B3D"),
    ( 1.00, "#1B5E20"),   # peak vegetation
]


def _hex_to_rgb(h: str) -> tuple[int, int, int]:
    h = h.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _build_lut(n: int = 512) -> np.ndarray:
    """
    (n, 3) uint8 lookup table over NDVI [-0.20, 1.00], linearly
    interpolated between NDVI_STOPS exactly as ndviToColor() does.
    """
    lo, hi = NDVI_STOPS[0][0], NDVI_STOPS[-1][0]
    xs = np.linspace(lo, hi, n)
    stop_v = np.array([s[0] for s in NDVI_STOPS])
    stop_rgb = np.array([_hex_to_rgb(s[1]) for s in NDVI_STOPS], dtype=float)
    lut = np.empty((n, 3), dtype=np.uint8)
    for c in range(3):
        lut[:, c] = np.round(np.interp(xs, stop_v, stop_rgb[:, c])).astype(np.uint8)
    return lut


_LUT = _build_lut()


def colorize(ndvi: np.ndarray) -> np.ndarray:
    """NDVI float array -> (h, w, 3) uint8 using the app's ramp."""
    lo, hi = NDVI_STOPS[0][0], NDVI_STOPS[-1][0]
    v = np.clip(np.nan_to_num(ndvi, nan=lo), lo, hi)
    idx = np.round((v - lo) / (hi - lo) * (_LUT.shape[0] - 1)).astype(np.int32)
    return _LUT[idx]


def _output_shape(bounds: Tuple[float, float, float, float],
                  lat: float) -> Tuple[int, int]:
    """
    Pixel grid for the WGS84 bbox, preserving aspect ratio on the ground.
    Longitude degrees shrink by cos(lat), so a naive degree-square grid
    would stretch the image east-west.
    """
    w, s, e, n = bounds
    dx = max((e - w) * np.cos(np.radians(lat)), 1e-12)
    dy = max(n - s, 1e-12)
    if dx >= dy:
        width = NDVI_IMAGE_MAX_PX
        height = int(round(NDVI_IMAGE_MAX_PX * dy / dx))
    else:
        height = NDVI_IMAGE_MAX_PX
        width = int(round(NDVI_IMAGE_MAX_PX * dx / dy))
    return max(width, NDVI_IMAGE_MIN_PX), max(height, NDVI_IMAGE_MIN_PX)


def render_ndvi_png(ndvi: np.ndarray,
                    visible: np.ndarray,
                    src_transform,
                    src_crs,
                    geom_wgs84) -> Optional[Tuple[bytes, dict]]:
    """
    Warp the NDVI array to the polygon's WGS84 bbox and encode a PNG.

    ndvi        : NDVI on the 10 m reference grid (NaN where invalid).
    visible     : bool/float mask of cells that may be drawn - pass the
                  crop-coverage weights, so cloud, shadow, non-crop and
                  out-of-polygon cells are all excluded.
    geom_wgs84  : the MEASUREMENT polygon (shapely, EPSG:4326).

    Returns (png_bytes, meta) or None when nothing is drawable.
    meta carries the bounds the image must be pinned to, so a consumer
    never has to guess the extent.
    """
    vis = np.asarray(visible, dtype="float32") > 0
    if not vis.any():
        return None

    src = np.where(vis & np.isfinite(ndvi), ndvi, np.nan).astype("float32")

    w, s, e, n = geom_wgs84.bounds
    width, height = _output_shape((w, s, e, n), (s + n) / 2.0)
    dst_transform = from_bounds(w, s, e, n, width, height)

    dst = np.full((height, width), np.nan, dtype="float32")
    reproject(
        source=src, destination=dst,
        src_transform=src_transform, src_crs=src_crs,
        dst_transform=dst_transform, dst_crs="EPSG:4326",
        src_nodata=np.nan, dst_nodata=np.nan,
        resampling=Resampling.nearest,        # never invent a value
    )

    drawn = np.isfinite(dst)
    if not drawn.any():
        return None

    rgba = np.zeros((height, width, 4), dtype=np.uint8)
    rgba[..., :3] = colorize(dst)
    rgba[..., 3] = np.where(drawn, 255, 0).astype(np.uint8)

    buf = io.BytesIO()
    Image.fromarray(rgba, mode="RGBA").save(buf, format="PNG", optimize=True)

    meta = {
        "width": width,
        "height": height,
        "crs": "EPSG:4326",
        "bounds_wgs84": {"west": w, "south": s, "east": e, "north": n},
        "placement": "north-up image stretched across bounds_wgs84",
        "drawn_pixels": int(drawn.sum()),
        "colormap": "ndviScience.NDVI_COLOR_STOPS",
        "value_range": [NDVI_STOPS[0][0], NDVI_STOPS[-1][0]],
        "resampling": "nearest",
        "bytes": buf.tell(),
    }
    return buf.getvalue(), meta


def storage_path(tenant_id: str, land_id: str,
                 acquisition_date: str, scene_id: str) -> str:
    """
    One object per OBSERVATION, not per land.

    v1 wrote "{land_id}.png" and overwrote it every run, which is why
    NDVIMapView has to cache-bust by scene_id and why an August metric
    could sit beside a June picture. Keying on the acquisition means
    every ndvi_data row points at the image that row was computed from,
    permanently.

    tenant_id FIRST: the storage RLS policy authorises on
    (storage.foldername(name))[1], so the tenant segment is what makes
    the bucket safe to make private.
    """
    safe_scene = "".join(c if c.isalnum() or c in "-_" else "_" for c in (scene_id or "noscene"))
    return f"{tenant_id}/{land_id}/{acquisition_date}_{safe_scene}.png"


def upload_png(client, path: str, data: bytes) -> Optional[str]:
    """
    Upload to the PRIVATE bucket and return the OBJECT PATH (not a URL).

    The path, not a URL, is what goes into ndvi_data.image_url: a signed
    URL expires, so persisting one would store a value that is wrong
    within the hour. The app mints a signed URL on read. Legacy rows
    still hold absolute public URLs, so any consumer must accept both -
    "starts with http" means legacy, anything else is a path to sign.

    upsert=True so a re-run of the same acquisition overwrites its own
    object rather than erroring; the path is unique per observation, so
    this can never overwrite a different observation's image.
    """
    try:
        client.storage.from_(NDVI_IMAGE_BUCKET).upload(
            path=path,
            file=data,
            file_options={"content-type": "image/png",
                          "cache-control": "3600",
                          "upsert": "true"},
        )
        return path
    except Exception as e:
        # Never fail a land over its picture: the numbers are the product,
        # the image is a presentation aid. Logged loudly, row still written.
        logger.warning(f"NDVI image upload failed for {path}: {type(e).__name__}: {e}")
        return None
