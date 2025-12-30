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
    """
    Build NDVI database row with safe None handling.
    
    CRITICAL FIX: Handle None values for mcari_trend and mcari_mean
    """
    
    # ============================================================
    # SAFE VALUE EXTRACTION with None handling
    # ============================================================
    def safe_round(value, decimals):
        """Round value if not None, otherwise return None"""
        return round(value, decimals) if value is not None else None
    
    # Extract values with defaults
    mcari_value = result.get("mcari_mean")
    mcari_trend_value = result.get("mcari_trend")
    
    return {
        "land_id": land["id"],
        "tenant_id": land["tenant_id"],

        # DATE ONLY (schema-aligned)
        "date": date.today().isoformat(),

        # NDVI metrics
        "ndvi_value": round(result["ndvi_mean"], 3),
        "min_ndvi": round(result["ndvi_min"], 3),
        "max_ndvi": round(result["ndvi_max"], 3),
        
        # NEW: Statistical metrics (with None handling)
        "ndvi_std": safe_round(result.get("ndvi_std"), 4),
        "median_ndvi": safe_round(result.get("median_ndvi"), 3),

        # Water stress
        "ndwi_value": safe_round(result.get("ndwi_mean"), 3),
        
        # NEW: MCARI - Chlorophyll/Nitrogen indicator (CRITICAL: None-safe)
        "mcari_value": safe_round(mcari_value, 3),

        # SAR soil moisture (nullable)
        "soil_moisture": result.get("soil_moisture"),

        # Thumbnail
        "image_url": result.get("ndvi_thumbnail_url"),
        
        # NEW: Quality metrics
        "valid_pixels": result.get("valid_pixels"),
        "total_pixels": result.get("total_pixels"),
        "coverage_percentage": result.get("coverage_percentage"),

        # Analytics metadata (CRITICAL: None-safe rounding)
        "metadata": {
            "ndvi_trend": safe_round(result["ndvi_trend"], 4),
            "ndre_trend": safe_round(result.get("ndre_trend", 0.0), 4),
            "mcari_trend": safe_round(mcari_trend_value, 4),  # FIX: None-safe
            "health_label": health_label,
            "alerts": alerts,
            "valid_observations": result["valid_observations"],
            "ndvi_geotiff_url": result.get("ndvi_geotiff_url"),
            "soil_moisture_error": result.get("soil_moisture_error"),
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

    # Statistics
    success_count = 0
    failed_count = 0
    skipped_count = 0

    for idx, land in enumerate(lands, 1):
        land_id = land["id"]
        tenant_id = land["tenant_id"]
        start_time = datetime.now(UTC)

        logger.info(
            f"\n[{idx}/{len(lands)}] Processing land {land_id}"
        )

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
                skipped_count += 1
                logger.warning(f"⏭️  Land {land_id} skipped (insufficient data)")
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
                mcari_mean=result.get("mcari_mean"),
                mcari_trend=result.get("mcari_trend"),
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
                    "ndvi_mean": result["ndvi_mean"],
                    "mcari_mean": result.get("mcari_mean"),
                    "health_label": health_label,
                }
            )
            
            success_count += 1
            logger.info(f"✅ Land {land_id} processed successfully")

        except Exception as e:
            logger.exception(f"❌ Land processing failed: {land_id}")

            mark_land_ndvi_failed(land_id=land_id)

            log_ndvi_step(
                processing_step="PROCESS_ERROR",
                step_status="failed",
                tenant_id=tenant_id,
                land_id=land_id,
                started_at=start_time,
                error_message=str(e),
            )
            
            failed_count += 1

    # --------------------------------------------------
    # Final summary
    # --------------------------------------------------
    logger.info("\n" + "="*60)
    logger.info("NDVI PIPELINE FINISHED")
    logger.info("="*60)
    logger.info(f"Total lands: {len(lands)}")
    logger.info(f"✅ Success: {success_count}")
    logger.info(f"⏭️  Skipped: {skipped_count}")
    logger.info(f"❌ Failed: {failed_count}")
    logger.info("="*60)


if __name__ == "__main__":
    main()
