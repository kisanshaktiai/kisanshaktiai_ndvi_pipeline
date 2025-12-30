#!/usr/bin/env python3
"""
ndvi_escalation_worker.py
-------------------------

Escalation worker for processing lands with pending/failed NDVI status.

This worker:
1. Fetches lands with ndvi_status = 'pending' or 'failed'
2. Attempts NDVI processing for each land
3. Updates status based on results
4. Designed to run after main.py to catch missed lands

Run manually or via GitHub Actions:
    python ndvi_escalation_worker.py
"""

import os
import sys
from datetime import date, datetime, UTC
from typing import List, Dict

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from db import (
    get_supabase_client,
    insert_ndvi,
    update_land_ndvi_snapshot,
    mark_land_ndvi_failed,
    log_ndvi_step,
)
from processor import process_land
from analysis import crop_health
from logger import logger


# --------------------------------------------------
# Fetch lands with pending/failed NDVI status
# --------------------------------------------------
def fetch_pending_lands(limit: int = 100) -> List[Dict]:
    """
    Fetch lands that need NDVI processing:
    - ndvi_status = 'pending' (never processed)
    - ndvi_status = 'failed' (previous failure)
    - ndvi_tested = false (not yet attempted)
    """
    supabase = get_supabase_client()
    
    try:
        # Query lands needing processing
        response = (
            supabase.table("lands")
            .select(
                "id, tenant_id, area_guntas, current_crop, boundary_polygon_old, "
                "ndvi_status, last_processed_at"
            )
            .eq("is_active", True)
            .is_("deleted_at", None)
            .or_(
                "ndvi_status.eq.pending,"
                "ndvi_status.eq.failed,"
                "ndvi_tested.eq.false"
            )
            .order("last_processed_at", desc=False)  # Oldest first
            .limit(limit)
            .execute()
        )
        
        lands = response.data or []
        
        logger.info(
            f"📦 Processing batch offset=0, count={len(lands)}"
        )
        
        return lands
        
    except Exception as e:
        logger.exception("Failed to fetch pending lands")
        return []


# --------------------------------------------------
# Build NDVI database row
# --------------------------------------------------
def build_ndvi_row(
    land: Dict,
    result: Dict,
    health_label: str,
    alerts: List[str],
) -> Dict:
    """Build NDVI time-series row for database insertion."""
    
    return {
        "land_id": land["id"],
        "tenant_id": land["tenant_id"],
        "date": date.today().isoformat(),
        
        # Core NDVI metrics
        "ndvi_value": round(result["ndvi_mean"], 3),
        "min_ndvi": round(result["ndvi_min"], 3),
        "max_ndvi": round(result["ndvi_max"], 3),
        
        # Statistical metrics
        "ndvi_std": round(result["ndvi_std"], 4) if result.get("ndvi_std") else None,
        "median_ndvi": round(result["median_ndvi"], 3) if result.get("median_ndvi") else None,
        
        # Water stress
        "ndwi_value": round(result["ndwi_mean"], 3) if result.get("ndwi_mean") else None,
        
        # Chlorophyll/Nitrogen (MCARI)
        "mcari_value": round(result["mcari_mean"], 3) if result.get("mcari_mean") else None,
        
        # Soil moisture
        "soil_moisture": result.get("soil_moisture"),
        
        # Files
        "image_url": result.get("ndvi_thumbnail_url"),
        
        # Quality metrics
        "valid_pixels": result.get("valid_pixels"),
        "total_pixels": result.get("total_pixels"),
        "coverage_percentage": result.get("coverage_percentage"),
        
        # Metadata
        "metadata": {
            "ndvi_trend": round(result["ndvi_trend"], 4),
            "ndre_trend": round(result.get("ndre_trend", 0.0), 4),
            "mcari_trend": round(result.get("mcari_trend", 0.0), 4),
            "health_label": health_label,
            "alerts": alerts,
            "valid_observations": result["valid_observations"],
            "ndvi_geotiff_url": result.get("ndvi_geotiff_url"),
            "soil_moisture_error": result.get("soil_moisture_error"),
            "processed_by": "escalation_worker",
        },
        
        # Processing info
        "computed_at": datetime.now(UTC).isoformat(),
        "satellite_source": "sentinel-2",
        "collection_id": "sentinel-2-l2a",
        "processing_level": "L2A",
        "spatial_resolution": 10,
    }


# --------------------------------------------------
# Main escalation worker
# --------------------------------------------------
def main():
    """
    Main escalation worker loop.
    Processes lands with pending/failed NDVI status.
    """
    
    logger.info("🚜 NDVI escalation worker started")
    
    # Get Supabase client for storage uploads
    supabase = get_supabase_client()
    
    # Fetch pending lands
    lands = fetch_pending_lands()
    
    if not lands:
        logger.info("✅ No pending lands found - all caught up!")
        return 0
    
    # Statistics
    success_count = 0
    failed_count = 0
    
    for idx, land in enumerate(lands, 1):
        land_id = land["id"]
        tenant_id = land["tenant_id"]
        current_status = land.get("ndvi_status", "unknown")
        
        logger.info(
            f"\n[{idx}/{len(lands)}] Processing land {land_id} "
            f"(Status: {current_status})"
        )
        
        start_time = datetime.now(UTC)
        
        try:
            # --------------------------------------------------
            # LOG: START
            # --------------------------------------------------
            log_ndvi_step(
                processing_step="ESCALATION_START",
                step_status="started",
                tenant_id=tenant_id,
                land_id=land_id,
                started_at=start_time,
                metadata={"previous_status": current_status},
            )
            
            # --------------------------------------------------
            # PROCESS LAND
            # --------------------------------------------------
            result = process_land(land, supabase)
            
            if result is None:
                logger.warning(
                    f"⏳ NDVI pending (no grid data yet) for land {land_id}"
                )
                
                log_ndvi_step(
                    processing_step="ESCALATION_PENDING",
                    step_status="skipped",
                    tenant_id=tenant_id,
                    land_id=land_id,
                    started_at=start_time,
                    metadata={"reason": "insufficient_satellite_data"},
                )
                continue
            
            # --------------------------------------------------
            # HEALTH CLASSIFICATION
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
            # INSERT NDVI DATA
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
                processing_step="ESCALATION_SUCCESS",
                step_status="completed",
                tenant_id=tenant_id,
                land_id=land_id,
                started_at=start_time,
                metadata={
                    "ndvi_mean": result["ndvi_mean"],
                    "health_label": health_label,
                    "previous_status": current_status,
                },
            )
            
            success_count += 1
            logger.info(f"✅ Land {land_id} processed successfully (NDVI={result['ndvi_mean']:.3f})")
            
        except Exception as e:
            logger.exception(f"❌ Land processing failed: {land_id}")
            
            # Mark as failed
            mark_land_ndvi_failed(land_id=land_id)
            
            # Log error
            log_ndvi_step(
                processing_step="ESCALATION_ERROR",
                step_status="failed",
                tenant_id=tenant_id,
                land_id=land_id,
                started_at=start_time,
                error_message=str(e),
                metadata={"previous_status": current_status},
            )
            
            failed_count += 1
    
    # --------------------------------------------------
    # Summary
    # --------------------------------------------------
    logger.info("\n" + "="*60)
    logger.info("✅ NDVI escalation worker finished")
    logger.info(f"   Lands processed: {success_count}")
    logger.info(f"   Lands failed: {failed_count}")
    logger.info(f"   Lands still pending: {len(lands) - success_count - failed_count}")
    logger.info("="*60)
    
    return 0 if failed_count == 0 else 1


if __name__ == "__main__":
    exit(main())
