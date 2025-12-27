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
from ndvi_geotiff import write_ndvi_geotiff
from config import MIN_VALID_PIXELS
from logger import logger


def process_land(land: dict, supabase) -> dict | None:
    """
    Process land for NDVI calculation, thumbnail generation, and GeoTIFF export.
    
    Args:
        land: Land record from database
        supabase: Supabase client for storage uploads
        
    Returns:
        Dictionary with NDVI metrics and file URLs, or None if processing failed
    """
    
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
    mcari_series: list[float] = []  # NEW: Track MCARI

    ndvi_raster = None
    ndvi_transform = None
    
    # Track statistics across all observations
    all_valid_pixels = []
    all_total_pixels = []

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

            # Compute indices
            indices = compute_indices(bands)
            ndvi = indices["NDVI"]
            ndre = indices["NDRE"]
            ndwi = indices["NDWI"]
            mcari = indices["MCARI"]  # NEW: Extract MCARI

            valid = np.isfinite(ndvi)
            valid_pixels = np.count_nonzero(valid)
            total_pixels = ndvi.size

            if valid_pixels < MIN_VALID_PIXELS:
                continue

            ndvi_series.append(float(np.nanmean(ndvi)))
            ndre_series.append(float(np.nanmean(ndre)))
            ndwi_series.append(float(np.nanmean(ndwi)))
            mcari_series.append(float(np.nanmean(mcari)))  # NEW: Track MCARI
            
            # Track pixel statistics
            all_valid_pixels.append(valid_pixels)
            all_total_pixels.append(total_pixels)

            # Save first valid raster for thumbnail and GeoTIFF
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
    # 3. NDVI thumbnail (PNG + upload to Supabase)
    # --------------------------------------------------
    ndvi_thumbnail_url = None
    if ndvi_raster is not None:
        try:
            public_url, png_path, json_path = generate_ndvi_thumbnail(
                ndvi_array=ndvi_raster,
                transform=ndvi_transform,
                land_id=land["id"],
                supabase=supabase,
            )
            ndvi_thumbnail_url = public_url
            
        except Exception as e:
            logger.warning(
                f"NDVI thumbnail failed for land {land['id']}: {e}"
            )

    # --------------------------------------------------
    # 4. NDVI GeoTIFF (full resolution + upload to Supabase)
    # --------------------------------------------------
    ndvi_geotiff_url = None
    if ndvi_raster is not None and ndvi_transform is not None:
        try:
            public_url, local_path = write_ndvi_geotiff(
                ndvi_array=ndvi_raster,
                transform=ndvi_transform,
                land_id=land["id"],
                supabase=supabase,
            )
            ndvi_geotiff_url = public_url
            
        except Exception as e:
            logger.warning(
                f"NDVI GeoTIFF failed for land {land['id']}: {e}"
            )

    # --------------------------------------------------
    # 5. Sentinel-1 SAR soil moisture (ENHANCED)
    # --------------------------------------------------
    soil_moisture_value = None
    s1_error_message = None
    
    try:
        logger.debug(f"Fetching Sentinel-1 data for land {land['id']}")
        s1_items = fetch_s1_items(geom)
        
        if not s1_items:
            logger.info(
                f"No Sentinel-1 data available for land {land['id']} "
                f"in lookback window. Soil moisture will be NULL."
            )
            s1_error_message = "No Sentinel-1 data in lookback window"
        else:
            logger.debug(f"Found {len(s1_items)} Sentinel-1 scenes for land {land['id']}")
            s1 = s1_items[0]
            
            # Check if VV/VH assets exist
            if "VV" not in s1.assets or "VH" not in s1.assets:
                logger.warning(
                    f"Sentinel-1 scene missing VV/VH polarization for land {land['id']}"
                )
                s1_error_message = "Missing VV/VH polarization"
            else:
                # Read SAR bands
                vv, _ = read_band(s1.assets["VV"], geom)
                vh, _ = read_band(s1.assets["VH"], geom)
                
                # Calculate soil moisture
                soil_moisture_value = soil_moisture(vv, vh)
                logger.info(
                    f"Soil moisture calculated for land {land['id']}: "
                    f"{soil_moisture_value:.2f} dB"
                )
                
    except Exception as e:
        logger.warning(
            f"Sentinel-1 processing failed for land {land['id']}: {e}"
        )
        s1_error_message = str(e)
        # Don't fail the entire pipeline - continue without soil moisture

    # --------------------------------------------------
    # 6. Calculate comprehensive statistics
    # --------------------------------------------------
    ndvi_mean = float(np.nanmean(ndvi_series))
    ndvi_min = float(np.nanmin(ndvi_series))
    ndvi_max = float(np.nanmax(ndvi_series))
    ndvi_trend_value = trend(ndvi_series)
    
    # NEW: Additional statistics from raster
    ndvi_std = None
    median_ndvi = None
    coverage_percentage = None
    valid_pixels_final = None
    total_pixels_final = None
    
    if ndvi_raster is not None:
        valid_mask = np.isfinite(ndvi_raster)
        valid_pixels_final = int(np.count_nonzero(valid_mask))
        total_pixels_final = int(ndvi_raster.size)
        
        if valid_pixels_final > 0:
            ndvi_std = float(np.nanstd(ndvi_raster))
            median_ndvi = float(np.nanmedian(ndvi_raster))
            coverage_percentage = round(
                (valid_pixels_final / total_pixels_final) * 100, 2
            )
    
    ndre_trend_value = trend(ndre_series)
    ndwi_mean = float(np.nanmean(ndwi_series))
    
    # NEW: MCARI statistics
    mcari_mean = float(np.nanmean(mcari_series))
    mcari_trend_value = trend(mcari_series)

    # --------------------------------------------------
    # 7. Return comprehensive results
    # --------------------------------------------------
    result = {
        # Core NDVI metrics
        "ndvi_mean": ndvi_mean,
        "ndvi_min": ndvi_min,
        "ndvi_max": ndvi_max,
        "ndvi_trend": ndvi_trend_value,
        
        # NEW: Statistical metrics
        "ndvi_std": ndvi_std,
        "median_ndvi": median_ndvi,
        
        # Supporting indices
        "ndre_trend": ndre_trend_value,
        "ndwi_mean": ndwi_mean,
        
        # NEW: MCARI metrics
        "mcari_mean": mcari_mean,
        "mcari_trend": mcari_trend_value,
        
        # Soil moisture (may be None)
        "soil_moisture": soil_moisture_value,
        "soil_moisture_error": s1_error_message,
        
        # File URLs
        "ndvi_thumbnail_url": ndvi_thumbnail_url,
        "ndvi_geotiff_url": ndvi_geotiff_url,
        
        # Quality metrics
        "valid_observations": len(ndvi_series),
        "valid_pixels": valid_pixels_final,
        "total_pixels": total_pixels_final,
        "coverage_percentage": coverage_percentage,
    }
    
    logger.debug(
        f"Land {land['id']} processing complete: "
        f"NDVI={ndvi_mean:.3f}, trend={ndvi_trend_value:.4f}, "
        f"coverage={coverage_percentage}%, soil_moisture={soil_moisture_value}"
    )
    
    return result
