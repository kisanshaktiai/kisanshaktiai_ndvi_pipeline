import numpy as np
import rasterio
from shapely.geometry import shape

from sentinel2_pc import fetch_s2_items
from sentinel1_pc import fetch_s1_items
from raster_utils import read_band, cloud_mask
from indices import compute_indices
from sar_soil_moisture import soil_moisture
from analysis import trend
from ndvi_thumbnail_supabase import generate_ndvi_thumbnail
from config import MIN_VALID_PIXELS
from logger import logger
from ndvi_geotiff import write_ndvi_geotiff


def process_land(land: dict, supabase) -> dict | None:
    """
    Process NDVI for a single land parcel.
    
    Args:
        land: Land record with geometry and metadata
        supabase: Supabase client instance for storage uploads
        
    Returns:
        dict with NDVI metrics and thumbnail URL, or None if processing fails
    """
    # --------------------------------------------------
    # 1. Geometry validation
    # --------------------------------------------------
    if not land.get("boundary_polygon_old"):
        logger.warning(f"Land {land['id']} has no GeoJSON boundary")
        return None

    try:
        geom = shape(land["boundary_polygon_old"])
        
        # Validate geometry
        if not geom.is_valid:
            logger.error(f"Invalid geometry for land {land['id']}")
            return None
            
        if geom.area == 0:
            logger.error(f"Zero-area geometry for land {land['id']}")
            return None
            
    except Exception as e:
        logger.error(f"Geometry parsing failed for land {land['id']}: {e}")
        return None

    # --------------------------------------------------
    # 2. Sentinel-2 multi-temporal processing
    # --------------------------------------------------
    ndvi_series: list[float] = []
    ndre_series: list[float] = []
    ndwi_series: list[float] = []

    ndvi_raster = None
    ndvi_transform = None
    successful_scenes = 0

    s2_items = fetch_s2_items(geom)
    if not s2_items:
        logger.warning(f"No Sentinel-2 data for land {land['id']}")
        return None

    logger.info(f"Processing {len(s2_items)} Sentinel-2 scenes for land {land['id']}")

    for idx, item in enumerate(s2_items):
        try:
            # Reference 10m grid using B04 (Red band)
            B04, ref_transform = read_band(item.assets["B04"], geom)

            # Read all required bands at 10m resolution
            bands = {
                "B04": B04,  # Red (10m)
                "B08": read_band(item.assets["B08"], geom, (B04, ref_transform))[0],  # NIR (10m)
                "B03": read_band(item.assets["B03"], geom, (B04, ref_transform))[0],  # Green (10m)
                "B02": read_band(item.assets["B02"], geom, (B04, ref_transform))[0],  # Blue (10m)
                "B05": read_band(item.assets["B05"], geom, (B04, ref_transform))[0],  # Red Edge (20m→10m)
                "SCL": read_band(item.assets["SCL"], geom, (B04, ref_transform))[0],  # Scene Classification (20m→10m)
            }

            # Apply cloud masking using SCL
            bands = cloud_mask(bands)

            # Compute vegetation indices
            indices = compute_indices(bands)
            ndvi = indices["NDVI"]
            ndre = indices["NDRE"]
            ndwi = indices["NDWI"]

            # Quality check: count valid (non-NaN) pixels
            valid = np.isfinite(ndvi)
            valid_pixels = np.count_nonzero(valid)

            if valid_pixels < MIN_VALID_PIXELS:
                logger.debug(
                    f"Scene {idx+1}/{len(s2_items)} skipped: "
                    f"only {valid_pixels} valid pixels (minimum: {MIN_VALID_PIXELS})"
                )
                continue

            # Add to time series
            ndvi_mean = float(np.nanmean(ndvi))
            ndre_mean = float(np.nanmean(ndre))
            ndwi_mean = float(np.nanmean(ndwi))
            
            # Sanity check for valid NDVI range
            if -1.0 <= ndvi_mean <= 1.0:
                ndvi_series.append(ndvi_mean)
                ndre_series.append(ndre_mean)
                ndwi_series.append(ndwi_mean)
                successful_scenes += 1

                # Save most recent valid raster for thumbnail generation
                if ndvi_raster is None:
                    ndvi_raster = ndvi
                    ndvi_transform = ref_transform
                    
                logger.debug(
                    f"Scene {idx+1}/{len(s2_items)} processed: "
                    f"NDVI={ndvi_mean:.3f}, valid_pixels={valid_pixels}"
                )
            else:
                logger.warning(
                    f"Scene {idx+1} has invalid NDVI mean: {ndvi_mean:.3f}"
                )

        except Exception as e:
            logger.warning(
                f"Sentinel-2 scene {idx+1}/{len(s2_items)} failed for land {land['id']}: {e}"
            )
            continue

    # Minimum temporal observations check
    MIN_TEMPORAL_OBSERVATIONS = 2  # At least 2 dates for trend
    if len(ndvi_series) < MIN_TEMPORAL_OBSERVATIONS:
        logger.warning(
            f"Insufficient NDVI observations for land {land['id']}: "
            f"{len(ndvi_series)}/{MIN_TEMPORAL_OBSERVATIONS} required "
            f"(from {len(s2_items)} scenes)"
        )
        return None

    logger.info(
        f"Successfully processed {successful_scenes}/{len(s2_items)} scenes "
        f"for land {land['id']}"
    )

    # --------------------------------------------------
    # 3. Generate NDVI thumbnail and upload to Supabase
    # --------------------------------------------------
    ndvi_thumbnail_url = None
    thumbnail_metadata = None
    
    if ndvi_raster is not None and ndvi_transform is not None:
        try:
            ndvi_thumbnail_url, thumbnail_metadata = generate_ndvi_thumbnail(
                ndvi_array=ndvi_raster,
                transform=ndvi_transform,
                land_id=land["id"],
                tenant_id=land["tenant_id"],
                supabase=supabase,
            )
            logger.info(f"NDVI thumbnail generated for land {land['id']}")
        except Exception as e:
            logger.warning(
                f"NDVI thumbnail generation failed for land {land['id']}: {e}"
            )

    # --------------------------------------------------
    # 4. Generate NDVI GeoTIFF (optional, for GIS export)
    # --------------------------------------------------
    ndvi_geotiff_path = None
    if ndvi_raster is not None and ndvi_transform is not None:
        try:
            ndvi_geotiff_path = write_ndvi_geotiff(
                ndvi_array=ndvi_raster,
                transform=ndvi_transform,
                land_id=land["id"]
            )
            logger.info(f"NDVI GeoTIFF saved: {ndvi_geotiff_path}")
        except Exception as e:
            logger.warning(
                f"NDVI GeoTIFF generation failed for land {land['id']}: {e}"
            )

    # --------------------------------------------------
    # 5. Sentinel-1 SAR soil moisture (optional)
    # --------------------------------------------------
    soil_moisture_value = None
    try:
        s1_items = fetch_s1_items(geom)
        if s1_items:
            logger.info(f"Processing Sentinel-1 SAR for land {land['id']}")
            s1 = s1_items[0]  # Most recent SAR scene
            
            vv, _ = read_band(s1.assets["VV"], geom)
            vh, _ = read_band(s1.assets["VH"], geom)
            
            soil_moisture_value = soil_moisture(vv, vh)
            logger.info(f"Soil moisture computed: {soil_moisture_value:.2f} dB")
    except Exception as e:
        logger.warning(
            f"Sentinel-1 processing failed for land {land['id']}: {e}"
        )

    # --------------------------------------------------
    # 6. Compute final statistics
    # --------------------------------------------------
    ndvi_mean = float(np.nanmean(ndvi_series))
    ndvi_min = float(np.nanmin(ndvi_series))
    ndvi_max = float(np.nanmax(ndvi_series))
    ndvi_std = float(np.nanstd(ndvi_series))
    
    # Trend analysis (linear regression slope)
    ndvi_trend = trend(ndvi_series)
    ndre_trend = trend(ndre_series) if len(ndre_series) >= 2 else 0.0
    
    # Water stress indicator
    ndwi_mean = float(np.nanmean(ndwi_series)) if ndwi_series else None

    # Spatial variability (coefficient of variation)
    ndvi_cv = ndvi_std / ndvi_mean if ndvi_mean > 0 else 0.0

    # --------------------------------------------------
    # 7. Return comprehensive results
    # --------------------------------------------------
    result = {
        # Core NDVI metrics
        "ndvi_mean": ndvi_mean,
        "ndvi_min": ndvi_min,
        "ndvi_max": ndvi_max,
        "ndvi_std": ndvi_std,
        "ndvi_cv": ndvi_cv,
        
        # Temporal trends
        "ndvi_trend": ndvi_trend,
        "ndre_trend": ndre_trend,
        
        # Water stress
        "ndwi_mean": ndwi_mean,
        
        # Soil moisture (SAR)
        "soil_moisture": soil_moisture_value,
        
        # Outputs
        "ndvi_thumbnail_url": ndvi_thumbnail_url,
        "ndvi_geotiff_path": ndvi_geotiff_path,
        
        # Quality metrics
        "valid_observations": len(ndvi_series),
        "total_scenes_processed": len(s2_items),
        "successful_scenes": successful_scenes,
        
        # Metadata
        "thumbnail_metadata": thumbnail_metadata,
    }

    logger.info(
        f"Land {land['id']} processing complete: "
        f"NDVI={ndvi_mean:.3f}, trend={ndvi_trend:.4f}, "
        f"observations={len(ndvi_series)}"
    )

    return result
