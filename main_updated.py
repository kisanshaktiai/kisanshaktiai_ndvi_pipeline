from datetime import date, datetime, UTC
from typing import Dict, List

from db import (
    fetch_lands,
    insert_ndvi,
    update_land_ndvi_snapshot,
    mark_land_ndvi_failed,
    log_ndvi_step,
    supabase,  # Import the Supabase client
)

from processor_updated import process_land
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
    Build database row for NDVI time-series table.
    """
    return {
        "land_id": land["id"],
        "tenant_id": land["tenant_id"],

        # DATE ONLY (schema-aligned)
        "date": date.today().isoformat(),

        # NDVI metrics
        "ndvi_value": round(result["ndvi_mean"], 3),
        "min_ndvi": round(result["ndvi_min"], 3),
        "max_ndvi": round(result["ndvi_max"], 3),

        # Water stress
        "ndwi_value": round(result["ndwi_mean"], 3) if result.get("ndwi_mean") else None,

        # SAR soil moisture (nullable)
        "soil_moisture": round(result["soil_moisture"], 2) if result.get("soil_moisture") else None,

        # Thumbnail URL (now from Supabase Storage)
        "image_url": result.get("ndvi_thumbnail_url"),

        # Analytics metadata (JSON column)
        "metadata": {
            "ndvi_trend": round(result["ndvi_trend"], 4),
            "ndvi_std": round(result.get("ndvi_std", 0.0), 4),
            "ndvi_cv": round(result.get("ndvi_cv", 0.0), 4),
            "ndre_trend": round(result.get("ndre_trend", 0.0), 4),
            "health_label": health_label,
            "alerts": alerts,
            "valid_observations": result["valid_observations"],
            "total_scenes": result.get("total_scenes_processed", 0),
            "successful_scenes": result.get("successful_scenes", 0),
            "thumbnail_metadata": result.get("thumbnail_metadata"),
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
    """
    Main NDVI processing pipeline.
    
    Workflow:
    1. Fetch active lands from database
    2. For each land:
       - Fetch Sentinel-2 imagery
       - Compute NDVI time-series
       - Generate thumbnail → Upload to Supabase Storage
       - Classify crop health
       - Store results in database
    3. Log processing steps and errors
    """
    logger.info("=" * 60)
    logger.info("NDVI PIPELINE STARTED")
    logger.info("=" * 60)

    # Fetch lands for processing
    lands = fetch_lands()

    if not lands:
        logger.info("No active lands found for processing")
        return

    logger.info(f"Found {len(lands)} active lands for NDVI processing")

    # Statistics
    success_count = 0
    failed_count = 0
    skipped_count = 0

    for idx, land in enumerate(lands, 1):
        land_id = land["id"]
        tenant_id = land["tenant_id"]
        current_crop = land.get("current_crop", "Unknown")
        
        logger.info(
            f"\n[{idx}/{len(lands)}] Processing land {land_id} "
            f"(Tenant: {tenant_id}, Crop: {current_crop})"
        )
        
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
                skipped_count += 1
                logger.warning(f"Land {land_id} skipped (insufficient data)")
                continue

            # --------------------------------------------------
            # AGRONOMIC INTERPRETATION
            # --------------------------------------------------
            health_label, alerts = crop_health(
                ndvi_mean=result["ndvi_mean"],
                ndvi_trend=result["ndvi_trend"],
                ndre_trend=result.get("ndre_trend"),
                ndwi_mean=result.get("ndwi_mean"),
                soil_moisture=result.get("soil_moisture"),
            )

            logger.info(
                f"Health assessment: {health_label} "
                f"| Alerts: {len(alerts)} | NDVI: {result['ndvi_mean']:.3f}"
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
            logger.info(f"NDVI data inserted for land {land_id}")

            # --------------------------------------------------
            # UPDATE LAND SNAPSHOT
            # --------------------------------------------------
            update_land_ndvi_snapshot(
                land_id=land_id,
                ndvi_value=result["ndvi_mean"],
                ndvi_date=date.today(),
                thumbnail_url=result.get("ndvi_thumbnail_url"),
            )
            logger.info(f"Land snapshot updated for {land_id}")

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
                    "health_label": health_label,
                    "observations": result["valid_observations"],
                }
            )
            
            success_count += 1
            logger.info(f"✓ Land {land_id} processed successfully")

        except Exception as e:
            logger.exception(f"✗ Land processing failed: {land_id}")

            # Mark land as failed in database
            mark_land_ndvi_failed(land_id=land_id)

            # Log error
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
    logger.info("\n" + "=" * 60)
    logger.info("NDVI PIPELINE FINISHED")
    logger.info("=" * 60)
    logger.info(f"Total lands: {len(lands)}")
    logger.info(f"✓ Success: {success_count}")
    logger.info(f"⊘ Skipped: {skipped_count}")
    logger.info(f"✗ Failed: {failed_count}")
    logger.info("=" * 60)


if __name__ == "__main__":
    main()
