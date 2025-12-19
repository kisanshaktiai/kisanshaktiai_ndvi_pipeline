import numpy as np
import rasterio
from shapely.geometry import shape

from sentinel2_pc import fetch_s2_items
from sentinel1_pc import fetch_s1_items
from raster_utils import read_band, cloud_mask
from indices import compute_indices
from sar_soil_moisture import soil_moisture
from analysis import trend
from ndvi_thumbnail import generate_ndvi_thumbnail
from config import MIN_VALID_PIXELS
from logger import logger
from ndvi_geotiff import write_ndvi_geotiff


def process_land(land: dict) -> dict | None:
    # --------------------------------------------------
    # 1. Geometry
    # --------------------------------------------------
    if not land.get("boundary_polygon_old"):
        logger.warning(f"Land {land['id']} has no GeoJSON boundary")
        return None

    try:
        geom = shape(land["boundary_polygon_old"])
    except Exception as e:
        logger.error(f"Invalid geometry for land {land['id']}: {e}")
        return None

    # --------------------------------------------------
    # 2. Sentinel-2 processing
    # --------------------------------------------------
    ndvi_series: list[float] = []
    ndre_series: list[float] = []
    ndwi_series: list[float] = []

    ndvi_raster = None
    ndvi_transform = None

    s2_items = fetch_s2_items(geom)
    if not s2_items:
        logger.warning(f"No Sentinel-2 data for land {land['id']}")
        return None

    for item in s2_items:
        try:
            # Reference 10m grid
            B04, ref_transform = read_band(item.assets["B04"], geom)

            bands = {
                "B04": B04,
                "B08": read_band(item.assets["B08"], geom, (B04, ref_transform))[0],
                "B03": read_band(item.assets["B03"], geom, (B04, ref_transform))[0],
                "B02": read_band(item.assets["B02"], geom, (B04, ref_transform))[0],
                "B05": read_band(item.assets["B05"], geom, (B04, ref_transform))[0],
                "SCL": read_band(item.assets["SCL"], geom, (B04, ref_transform))[0],
            }

            # Cloud mask
            bands = cloud_mask(bands)

            # ✅ CORRECT index extraction
            indices = compute_indices(bands)
            ndvi = indices["NDVI"]
            ndre = indices["NDRE"]
            ndwi = indices["NDWI"]

            valid = np.isfinite(ndvi)
            valid_pixels = np.count_nonzero(valid)

            if valid_pixels < MIN_VALID_PIXELS:
                continue

            ndvi_series.append(float(np.nanmean(ndvi)))
            ndre_series.append(float(np.nanmean(ndre)))
            ndwi_series.append(float(np.nanmean(ndwi)))

            # Save first valid raster for thumbnail
            if ndvi_raster is None:
                ndvi_raster = ndvi
                ndvi_transform = ref_transform

        except Exception as e:
            logger.warning(
                f"Sentinel-2 scene skipped for land {land['id']}: {e}"
            )
            continue

    if len(ndvi_series) < 2:
        logger.warning(
            f"Insufficient NDVI observations for land {land['id']} "
            f"({len(ndvi_series)} dates)"
        )
        return None

    # --------------------------------------------------
    # 3. NDVI thumbnail
    # --------------------------------------------------
    ndvi_thumbnail_url = None
    if ndvi_raster is not None:
        try:
            ndvi_thumbnail_url = generate_ndvi_thumbnail(
                ndvi_array=ndvi_raster,
                transform=ndvi_transform,
                land_id=land["id"],
            )
        except Exception as e:
            logger.warning(
                f"NDVI thumbnail failed for land {land['id']}: {e}"
            )
            
            
    ndvi_geotiff_path = None

    if ndvi_raster is not None and ndvi_transform is not None:
        try:
            ndvi_geotiff_path = write_ndvi_geotiff(
                ndvi_array=ndvi_raster,
                transform=ndvi_transform,
                land_id=land["id"]
            )
        except Exception as e:
            logger.warning(
                f"NDVI GeoTIFF failed for land {land['id']}: {e}"
            )


    # --------------------------------------------------
    # 4. Sentinel-1 SAR soil moisture
    # --------------------------------------------------
    soil_moisture_value = None
    try:
        s1_items = fetch_s1_items(geom)
        if s1_items:
            s1 = s1_items[0]
            vv, _ = read_band(s1.assets["VV"], geom)
            vh, _ = read_band(s1.assets["VH"], geom)
            soil_moisture_value = soil_moisture(vv, vh)
    except Exception as e:
        logger.warning(
            f"Sentinel-1 processing failed for land {land['id']}: {e}"
        )

    # --------------------------------------------------
    # 5. Final statistics
    # --------------------------------------------------
    ndvi_mean = float(np.nanmean(ndvi_series))
    ndvi_min = float(np.nanmin(ndvi_series))
    ndvi_max = float(np.nanmax(ndvi_series))
    ndvi_trend = trend(ndvi_series)

    ndre_trend = trend(ndre_series)
    ndwi_mean = float(np.nanmean(ndwi_series))

    # --------------------------------------------------
    # 6. Return
    # --------------------------------------------------
    return {
        "ndvi_mean": ndvi_mean,
        "ndvi_min": ndvi_min,
        "ndvi_max": ndvi_max,
        "ndvi_trend": ndvi_trend,
        "ndre_trend": ndre_trend,
        "ndwi_mean": ndwi_mean,
        "soil_moisture": soil_moisture_value,
        "ndvi_thumbnail_url": ndvi_thumbnail_url,
        "valid_observations": len(ndvi_series),
    }
