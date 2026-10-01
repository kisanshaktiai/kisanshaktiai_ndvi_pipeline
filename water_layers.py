"""Observed water-related Sentinel-2 layers.

Creates surface_water_trace (MNDWI evidence) and canopy_moisture_signal
(NDMI using the Sentinel-2 B8A/B11 moisture-index combination).
No agronomic water-stress classification is performed here.
"""
from __future__ import annotations
import io
import numpy as np
from PIL import Image
from rasterio.transform import from_bounds
from rasterio.warp import reproject, Resampling
from config import NDVI_IMAGE_MAX_PX, NDVI_IMAGE_BUCKET
from db import supabase, with_retry

# Output version - part of the scene ledger identity (main.PROCESSING_IDENTITY).
# Bump it whenever the water layer algorithm (canopy moisture, surface water trace) changes, so every scene is re-evaluated.
WATER_LAYER_VERSION = "water-layers-v1"


def _ratio(a, b):
    den = a + b
    out = np.full_like(a, np.nan, dtype="float32")
    ok = np.isfinite(a) & np.isfinite(b) & (np.abs(den) > 1e-6)
    out[ok] = (a[ok] - b[ok]) / den[ok]
    return out


import functools


@functools.lru_cache(maxsize=16)
def _config(layer_code):
    """Layer display config. Cached for the run: it was re-read from the
    database for every layer of every scene of every land (visible as
    repeated satellite_layer_config requests in the 2026-09-23 log)."""
    return _config_uncached(layer_code)


def _config_uncached(layer_code):
    res = with_retry(lambda: supabase.table("satellite_layer_config").select(
        "value_min,value_max,evidence_min,color_stops"
    ).eq("layer_code", layer_code).eq("enabled", True).limit(1).execute(),
        what=f"water layer config {layer_code}", attempts=2)
    if not res.data:
        raise RuntimeError(f"No enabled satellite_layer_config for {layer_code}")
    return res.data[0]


def _rgb(values, stops):
    xs = np.array([float(s["v"]) for s in stops], dtype="float32")
    rgb = np.array([[int(s["c"][i:i+2], 16) for i in (1, 3, 5)] for s in stops], dtype=float)
    flat = values.reshape(-1)
    out = np.zeros((flat.size, 3), dtype=np.uint8)
    finite = np.isfinite(flat)
    clipped = np.clip(flat[finite], xs[0], xs[-1])
    for c in range(3):
        out[finite, c] = np.round(np.interp(clipped, xs, rgb[:, c])).astype(np.uint8)
    return out.reshape(values.shape + (3,))


def _render(values, visible, geom_wgs84, src_transform, src_crs, layer_code):
    cfg = _config(layer_code)
    w, s, e, n = geom_wgs84.bounds
    lat = (s + n) / 2.0
    dx = max((e - w) * np.cos(np.radians(lat)), 1e-12)
    dy = max(n - s, 1e-12)
    # Same display size as every other field image (config NDVI_IMAGE_MAX_PX).
    # It was a hard-coded 768 px: for 10 m source pixels that is 2.25x the
    # pixels to warp and compress for no extra information, and it made the
    # water stage the single most expensive step per land (~90 ms). Layer
    # statistics come from the source arrays, not this image, so no value
    # changes; drawn_pixels is only ever tested for being > 0.
    max_px = NDVI_IMAGE_MAX_PX
    if dx >= dy:
        width, height = max_px, max(64, int(round(max_px * dy / dx)))
    else:
        height, width = max_px, max(64, int(round(max_px * dx / dy)))
    dst_transform = from_bounds(w, s, e, n, width, height)

    # Continuous index values are retained for measurement/statistics, but the
    # farmer-facing surface-water layer must show only spatial evidence pixels.
    # The evidence cutoff is controlled by satellite_layer_config, never TS.
    evidence_min = cfg.get("evidence_min")
    if layer_code == "surface_water_trace" and evidence_min is not None:
        evidence = np.isfinite(values) & (values >= float(evidence_min))
    else:
        evidence = np.isfinite(values)

    src = np.where(np.asarray(visible, dtype=bool) & evidence, values, np.nan).astype("float32")
    dst = np.full((height, width), np.nan, dtype="float32")
    reproject(source=src, destination=dst, src_transform=src_transform, src_crs=src_crs,
              dst_transform=dst_transform, dst_crs="EPSG:4326", src_nodata=np.nan,
              dst_nodata=np.nan, resampling=Resampling.nearest)
    drawn = np.isfinite(dst)
    if not drawn.any():
        return None

    rgba = np.zeros((height, width, 4), dtype=np.uint8)
    rgba[..., :3] = _rgb(dst, cfg["color_stops"])
    # Non-evidence pixels are fully transparent, so the farmer sees the real
    # location of the signal over the satellite basemap instead of a gray tile.
    rgba[..., 3] = np.where(drawn, 235, 0).astype(np.uint8)
    buf = io.BytesIO()
    Image.fromarray(rgba, mode="RGBA").save(buf, format="PNG", compress_level=6)
    return buf.getvalue(), {
        "layer_code": layer_code,
        "width": width,
        "height": height,
        "crs": "EPSG:4326",
        "bounds_wgs84": {"west": w, "south": s, "east": e, "north": n},
        "resampling": "nearest",
        "drawn_pixels": int(drawn.sum()),
        "config_value_min": cfg["value_min"],
        "config_value_max": cfg["value_max"],
        "evidence_min": evidence_min,
        "render_semantics": "spatial_evidence_only" if layer_code == "surface_water_trace" else "continuous_observed_signal",
    }


def _upload(tenant_id, land_id, date, scene_id, layer_code, png):
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in scene_id)
    path = f"{tenant_id}/{land_id}/water/{date}_{safe}_{layer_code}.png"
    with_retry(lambda: supabase.storage.from_(NDVI_IMAGE_BUCKET).upload(
        path, png, {"content-type": "image/png", "upsert": "true"}),
        what=f"upload {layer_code} image {land_id}/{scene_id}", attempts=3)
    return path


def _water_path(tenant_id, land_id, date, scene_id, layer_code):
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in scene_id)
    return f"{tenant_id}/{land_id}/water/{date}_{safe}_{layer_code}.png"


def build_water_layers(*, land, item, bands, masks, geom_wgs84, ref_transform, ref_crs,
                       sink: list = None, uploads: list = None):
    """Observed water layers for one land and scene.

    sink / uploads (batched mode, main.persist_block): records are appended to
    `sink` and PNGs to `uploads` instead of being written here one by one. The
    caller writes a whole block's records in one batch and uploads its images
    concurrently; a failed image never loses the layer's numbers.
    Without them the function behaves exactly as before."""
    b03, b08, b8a, b11 = bands.get("B03"), bands.get("B08"), bands.get("B8A"), bands.get("B11")
    if b03 is None or b08 is None or b8a is None or b11 is None:
        return []
    visible = masks["in_field"] & ~masks["cloud"] & ~masks["shadow"] & ~masks["snow"]
    if not np.any(visible):
        return []
    mndwi, ndmi = _ratio(b03, b11), _ratio(b8a, b11)
    acquisition_date = item.datetime.date().isoformat()
    acquisition_time = item.datetime.isoformat() if item.datetime else None
    out = []
    for code, values, index, band_names in [
        ("surface_water_trace", mndwi, "MNDWI", ["B03", "B11"]),
        ("canopy_moisture_signal", ndmi, "NDMI", ["B8A", "B11"]),
    ]:
        finite = visible & np.isfinite(values)
        if not np.any(finite):
            continue
        v = values[finite].astype(float)
        rendered = _render(values, visible, geom_wgs84, ref_transform, ref_crs, code)
        if rendered and uploads is not None:
            path = _water_path(land["tenant_id"], land["id"], acquisition_date, item.id, code)
            uploads.append({"path": path, "data": rendered[0], "kind": "water"})
        else:
            path = _upload(land["tenant_id"], land["id"], acquisition_date, item.id, code, rendered[0]) if rendered else None
        rec = {
            "tenant_id": land["tenant_id"], "land_id": land["id"], "scene_id": item.id,
            "acquisition_date": acquisition_date, "acquisition_time": acquisition_time,
            "layer_code": code, "value_mean": float(np.nanmean(v)), "value_median": float(np.nanmedian(v)),
            "value_p10": float(np.nanpercentile(v, 10)), "value_p90": float(np.nanpercentile(v, 90)),
            "value_min": float(np.nanmin(v)), "value_max": float(np.nanmax(v)),
            "valid_fraction": float(np.count_nonzero(finite) / max(np.count_nonzero(masks["in_field"]), 1)),
            "effective_pixel_count": float(np.count_nonzero(finite)), "image_path": path,
            "image_metadata": rendered[1] if rendered else {"render_failed": True},
            "uncertainty_json": {"scope": "observed spatial support only", "model_confidence": None},
            "evidence_json": {
                "surface_water_scl_fraction": masks.get("water_fraction"),
                "cloud_fraction": masks.get("cloud_fraction"),
                "shadow_fraction": masks.get("shadow_fraction"),
                "snow_fraction": masks.get("snow_fraction"),
                "effective_pixel_count": masks.get("epc_total"),
                "observed_or_predicted": "observed",
                "spatial_evidence_cutoff": rendered[1].get("evidence_min") if rendered else None,
                "spatial_evidence_pixels": rendered[1].get("drawn_pixels") if rendered else 0,
            },
            "provenance_json": {
                "source": "sentinel-2-l2a", "index": index, "bands": band_names,
                "no_agronomic_threshold_applied": True,
                "spatial_render_is_evidence_mask": code == "surface_water_trace",
            },
            "status": "observed",
        }
        if sink is not None:
            sink.append(rec)
        else:
            with_retry(lambda r=rec: supabase.table("satellite_water_layers").upsert(
                r, on_conflict="tenant_id,land_id,scene_id,layer_code").execute(),
                what=f"upsert {code} {land['id']}/{item.id}", attempts=3)
        out.append(rec)
    return out
