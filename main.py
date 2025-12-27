from datetime import date, datetime, UTC
from typing import Dict, List

from db import (
    fetch_lands,
    insert_ndvi,
    update_land_ndvi_snapshot,
    mark_land_ndvi_failed,
    log_ndvi_step,
    get_supabase_client,
)

from processor import process_land
from analysis import crop_health
from logger import logger


# --------------------------------------------------
# Build NDVI time-series row
# --------------------------------------------------
def build_ndvi_row(
    *,
    land: Dict,
    result: Dict,
    health_label: str,
    alerts: List[str],
) -> Dict:

    return {
        "land_id": land["id"],
        "tenant_id": land["tenant_id"],

        # DATE ONLY (schema-aligned)
        "date": date.today().isoformat(),

        # NDVI metrics
        "ndvi_value": round(result["ndvi_mean"], 3),
        "min_ndvi": round(result["ndvi_min"], 3),
        "max_ndvi": round(result["ndvi_max"], 3),
        
        # NEW: Statistical metrics
        "ndvi_std": round(result["ndvi_std"], 4) if result.get("ndvi_std") else None,
        "median_ndvi": round(result["median_ndvi"], 3) if result.get("median_ndvi") else None,

        # Water stress
        "ndwi_value": round(result["ndwi_mean"], 3),
        
        # NEW: MCARI - Chlorophyll/Nitrogen indicator
        "mcari_value": round(result["mcari_mean"], 3) if result.get("mcari_mean") else None,

        # SAR soil moisture (nullable)
        "soil_moisture": result.get("soil_moisture"),

        # Thumbnail
        "image_url": result.get("ndvi_thumbnail_url"),
        
        # NEW: Quality metrics
        "valid_pixels": result.get("valid_pixels"),
        "total_pixels": result.get("total_pixels"),
        "coverage_percentage": result.get("coverage_percentage"),

        # Analytics metadata
        "metadata": {
            "ndvi_trend": round(result["ndvi_trend"], 4),
            "ndre_trend": round(result.get("ndre_trend", 0.0), 4),
            "mcari_trend": round(result.get("mcari_trend", 0.0), 4),  # NEW: MCARI trend
            "health_label": health_label,
            "alerts": alerts,
            "valid_observations": result["valid_observations"],
            "ndvi_geotiff_url": result.get("ndvi_geotiff_url"),
            "soil_moisture_error": result.get("soil_moisture_error"),  # Track S1 failures
        },

        # Processing info
        "computed_at": datetime.now(UTC).isoformat(),
        "satellite_source": "sentinel-2",
        "collection_id": "sentinel-2-l2a",
        "processing_level": "L2A",
        "spatial_resolution": 10,
    }


# --------------------------------------------------
# Main pipeline
# --------------------------------------------------
def main():
    logger.info("NDVI pipeline started")

    # Get Supabase client for storage uploads
    supabase = get_supabase_client()

    lands = fetch_lands()

    if not lands:
        logger.info("No active lands found")
        return

    for land in lands:
        land_id = land["id"]
        tenant_id = land["tenant_id"]
        start_time = datetime.now(UTC)

        try:
            # --------------------------------------------------
            # LOG: PROCESS START
            # --------------------------------------------------
            log_ndvi_step(
                processing_step="PROCESS_START",
                step_status="started",
                tenant_id=tenant_id,
                land_id=land_id,
                started_at=start_time,
            )

            # --------------------------------------------------
            # PROCESS LAND (with Supabase client for uploads)
            # --------------------------------------------------
            result = process_land(land, supabase)

            if result is None:
                log_ndvi_step(
                    processing_step="PROCESS_SKIPPED",
                    step_status="skipped",
                    tenant_id=tenant_id,
                    land_id=land_id,
                    started_at=start_time,
                )
                continue

            # --------------------------------------------------
            # AGRONOMIC INTERPRETATION (Enhanced with MCARI)
            # --------------------------------------------------
            health_label, alerts = crop_health(
                ndvi_mean=result["ndvi_mean"],
                ndvi_trend=result["ndvi_trend"],
                ndre_trend=result.get("ndre_trend"),
                ndwi_mean=result.get("ndwi_mean"),
                soil_moisture=result.get("soil_moisture"),
                mcari_mean=result.get("mcari_mean"),      # NEW: MCARI
                mcari_trend=result.get("mcari_trend"),    # NEW: MCARI trend
            )

            # --------------------------------------------------
            # INSERT NDVI TIME-SERIES
            # --------------------------------------------------
            ndvi_row = build_ndvi_row(
                land=land,
                result=result,
                health_label=health_label,
                alerts=alerts,
            )

            insert_ndvi(ndvi_row)

            # --------------------------------------------------
            # UPDATE LAND SNAPSHOT
            # --------------------------------------------------
            update_land_ndvi_snapshot(
                land_id=land_id,
                ndvi_value=result["ndvi_mean"],
                ndvi_date=date.today(),
                thumbnail_url=result.get("ndvi_thumbnail_url"),
                geotiff_url=result.get("ndvi_geotiff_url"),
            )

            # --------------------------------------------------
            # LOG: SUCCESS
            # --------------------------------------------------
            log_ndvi_step(
                processing_step="PROCESS_END",
                step_status="completed",
                tenant_id=tenant_id,
                land_id=land_id,
                started_at=start_time,
                metadata={
                    "thumbnail_url": result.get("ndvi_thumbnail_url"),
                    "geotiff_url": result.get("ndvi_geotiff_url"),
                    "health_label": health_label,
                }
            )

        except Exception as e:
            logger.exception(f"Land processing failed: {land_id}")

            mark_land_ndvi_failed(land_id=land_id)

            log_ndvi_step(
                processing_step="PROCESS_ERROR",
                step_status="failed",
                tenant_id=tenant_id,
                land_id=land_id,
                started_at=start_time,
                error_message=str(e),
            )

    logger.info("NDVI pipeline finished")


if __name__ == "__main__":
    main()
