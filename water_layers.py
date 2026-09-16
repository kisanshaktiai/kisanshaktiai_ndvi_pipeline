"""Observed water-related Sentinel-2 layers.

Creates surface_water_trace (MNDWI + SCL water evidence) and
canopy_moisture_signal (NDMI). No agronomic threshold or water-stress
classification is performed here; presentation ramps come from DB config.
"""
from __future__ import annotations
import io
import numpy as np
from PIL import Image
from rasterio.transform import from_bounds
from rasterio.warp import reproject, Resampling
from config import NDVI_IMAGE_BUCKET
from db import supabase, with_retry


def _ratio(a, b):
    den = a + b
    out = np.full_like(a, np.nan, dtype="float32")
    ok = np.isfinite(a) & np.isfinite(b) & (np.abs(den) > 1e-6)
    out[ok] = (a[ok] - b[ok]) / den[ok]
    return out


def _config(layer_code):
    res = with_retry(lambda: supabase.table("satellite_layer_config").select(
        "value_min,value_max,color_stops").eq("layer_code", layer_code).eq(
        "enabled", True).limit(1).execute(), what=f"water layer config {layer_code}", attempts=2)
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
    max_px = 768
    if dx >= dy:
        width, height = max_px, max(64, int(round(max_px * dy / dx)))
    else:
        height, width = max_px, max(64, int(round(max_px * dx / dy)))
    dst_transform = from_bounds(w, s, e, n, width, height)
    src = np.where(np.asarray(visible, dtype=bool) & np.isfinite(values), values, np.nan).astype("float32")
    dst = np.full((height, width), np.nan, dtype="float32")
    reproject(source=src, destination=dst, src_transform=src_transform, src_crs=src_crs,
              dst_transform=dst_transform, dst_crs="EPSG:4326", src_nodata=np.nan,
              dst_nodata=np.nan, resampling=Resampling.nearest)
    drawn = np.isfinite(dst)
    if not drawn.any():
        return None
    rgba = np.zeros((height, width, 4), dtype=np.uint8)
    rgba[..., :3] = _rgb(dst, cfg["color_stops"])
    rgba[..., 3] = np.where(drawn, 255, 0).astype(np.uint8)
    buf = io.BytesIO()
    Image.fromarray(rgba, mode="RGBA").save(buf, format="PNG", optimize=True)
    return buf.getvalue(), {"layer_code": layer_code, "width": width, "height": height,
        "crs": "EPSG:4326", "bounds_wgs84": {"west": w, "south": s, "east": e, "north": n},
        "resampling": "nearest", "drawn_pixels": int(drawn.sum()),
        "config_value_min": cfg["value_min"], "config_value_max": cfg["value_max"]}


def _upload(tenant_id, land_id, date, scene_id, layer_code, png):
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in scene_id)
    path = f"{tenant_id}/{land_id}/water/{date}_{safe}_{layer_code}.png"
    with_retry(lambda: supabase.storage.from_(NDVI_IMAGE_BUCKET).upload(
        path, png, {"content-type": "image/png", "upsert": "true"}),
        what=f"upload {layer_code} image {land_id}/{scene_id}", attempts=3)
    return path


def build_water_layers(*, land, item, bands, masks, geom_wgs84, ref_transform, ref_crs):
    b03, b08, b11 = bands.get("B03"), bands.get("B08"), bands.get("B11")
    if b03 is None or b08 is None or b11 is None:
        return []
    visible = masks["in_field"] & ~masks["cloud"] & ~masks["shadow"] & ~masks["snow"]
    if not np.any(visible):
        return []
    mndwi, ndmi = _ratio(b03, b11), _ratio(b08, b11)
    acquisition_date = item.datetime.date().isoformat()
    acquisition_time = item.datetime.isoformat() if item.datetime else None
    out = []
    for code, values, index, band_names in [
        ("surface_water_trace", mndwi, "MNDWI", ["B03", "B11"]),
        ("canopy_moisture_signal", ndmi, "NDMI", ["B08", "B11"]),
    ]:
        finite = visible & np.isfinite(values)
        if not np.any(finite):
            continue
        v = values[finite].astype(float)
        rendered = _render(values, visible, geom_wgs84, ref_transform, ref_crs, code)
        path = _upload(land["tenant_id"], land["id"], acquisition_date, item.id, code, rendered[0]) if rendered else None
        rec = {"tenant_id": land["tenant_id"], "land_id": land["id"], "scene_id": item.id,
            "acquisition_date": acquisition_date, "acquisition_time": acquisition_time,
            "layer_code": code, "value_mean": float(np.nanmean(v)), "value_median": float(np.nanmedian(v)),
            "value_p10": float(np.nanpercentile(v, 10)), "value_p90": float(np.nanpercentile(v, 90)),
            "value_min": float(np.nanmin(v)), "value_max": float(np.nanmax(v)),
            "valid_fraction": float(np.count_nonzero(finite) / max(np.count_nonzero(masks["in_field"]), 1)),
            "effective_pixel_count": float(np.count_nonzero(finite)), "image_path": path,
            "image_metadata": rendered[1] if rendered else {"render_failed": True},
            "uncertainty_json": {"scope": "observed spatial support only", "model_confidence": None},
            "evidence_json": {"surface_water_scl_fraction": masks.get("water_fraction"),
                "cloud_fraction": masks.get("cloud_fraction"), "shadow_fraction": masks.get("shadow_fraction"),
                "snow_fraction": masks.get("snow_fraction"), "effective_pixel_count": masks.get("epc_total"),
                "observed_or_predicted": "observed"},
            "provenance_json": {"source": "sentinel-2-l2a", "index": index, "bands": band_names,
                "no_agronomic_threshold_applied": True}, "status": "observed"}
        with_retry(lambda r=rec: supabase.table("satellite_water_layers").upsert(
            r, on_conflict="tenant_id,land_id,scene_id,layer_code").execute(),
            what=f"upsert {code} {land['id']}/{item.id}", attempts=3)
        out.append(rec)
    return out
