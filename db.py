"""
db.py
-----

Supabase database access layer for KisanShaktiAI NDVI pipeline.

• Schema-aligned with `lands`, `ndvi_data`, `ndvi_processing_logs`
• Safe for re-runs (idempotent writes)
• Logging failures never break pipeline
• Multi-tenant aware
• UTC-safe timestamps
"""

import os
from typing import List, Dict, Optional
from datetime import date, datetime, UTC

from supabase import create_client, Client
from dotenv import load_dotenv
from logger import logger

# --------------------------------------------------
# Environment & Client
# --------------------------------------------------
load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

if not SUPABASE_URL or not SUPABASE_KEY:
    raise RuntimeError(
        "Supabase credentials not set. "
        "Ensure SUPABASE_URL and SUPABASE_KEY are defined."
    )

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# --------------------------------------------------
# Fetch active lands for NDVI processing
# --------------------------------------------------
def fetch_lands(limit: int = 100) -> List[Dict]:
    """
    Fetch active, non-deleted lands eligible for NDVI processing.
    """
    try:
        res = (
            supabase.table("lands")
            .select(
                "id, tenant_id, area_guntas, current_crop, boundary_polygon_old"
            )
            .eq("is_active", True)
            .is_("deleted_at", None)
            .limit(limit)
            .execute()
        )
        return res.data or []

    except Exception:
        logger.exception("Failed to fetch lands")
        return []

# --------------------------------------------------
# NDVI time-series insert (IDEMPOTENT)
# --------------------------------------------------
def insert_ndvi(row: Dict) -> None:
    """
    Insert or update NDVI time-series data.
    Enforced uniqueness: (land_id, date)
    """
    try:
        (
            supabase.table("ndvi_data")
            .upsert(
                row,
                on_conflict="land_id,date",
            )
            .execute()
        )

    except Exception:
        logger.exception(
            f"NDVI insert failed for land {row.get('land_id')}"
        )
        raise

# --------------------------------------------------
# Update land snapshot (LATEST NDVI ONLY)
# --------------------------------------------------
def update_land_ndvi_snapshot(
    *,
    land_id: str,
    ndvi_value: float,
    ndvi_date: date,
    thumbnail_url: Optional[str],
    geotiff_url: Optional[str] = None,
) -> None:
    """
    Update latest NDVI snapshot & processing metadata in `lands`.

    Matches schema exactly:
    - last_ndvi_value
    - last_ndvi_calculation
    - ndvi_thumbnail_url
    - ndvi_geotiff_url (optional)
    - ndvi_tested
    - ndvi_status
    """
    try:
        update_data = {
            "last_ndvi_value": round(ndvi_value, 3),
            "last_ndvi_calculation": ndvi_date.isoformat(),
            "ndvi_thumbnail_url": thumbnail_url,
            "ndvi_tested": True,
            "ndvi_status": "completed",
            "last_processed_at": datetime.now(UTC).isoformat(),
            "updated_at": datetime.now(UTC).isoformat(),
        }
        
        # Add GeoTIFF URL if provided
        if geotiff_url:
            update_data["ndvi_geotiff_url"] = geotiff_url
        
        (
            supabase.table("lands")
            .update(update_data)
            .eq("id", land_id)
            .execute()
        )

    except Exception:
        logger.exception(f"Failed to update NDVI snapshot for land {land_id}")
        raise

# --------------------------------------------------
# Update land status on failure
# --------------------------------------------------
def mark_land_ndvi_failed(
    *,
    land_id: str,
) -> None:
    """
    Mark NDVI processing as failed for a land.
    """
    try:
        (
            supabase.table("lands")
            .update(
                {
                    "ndvi_status": "failed",
                    "last_processed_at": datetime.now(UTC).isoformat(),
                    "updated_at": datetime.now(UTC).isoformat(),
                }
            )
            .eq("id", land_id)
            .execute()
        )

    except Exception:
        logger.warning(f"Failed to mark land NDVI as failed: {land_id}")

# --------------------------------------------------
# NDVI processing logs (OBSERVABILITY)
# --------------------------------------------------
def log_ndvi_step(
    *,
    processing_step: str,
    step_status: str,
    tenant_id: str,
    land_id: Optional[str] = None,
    satellite_tile_id: Optional[str] = None,
    started_at: Optional[datetime] = None,
    error_message: Optional[str] = None,
    error_details: Optional[Dict] = None,
    metadata: Optional[Dict] = None,
) -> None:
    """
    Insert NDVI processing log entry.

    • Fully schema-aligned with `ndvi_processing_logs`
    • Logging failures NEVER break pipeline
    • Enhanced error handling with detailed logging
    """
    try:
        now = datetime.now(UTC)

        payload = {
            "processing_step": processing_step,
            "step_status": step_status,
            "tenant_id": tenant_id,
            "land_id": land_id,
            "satellite_tile_id": satellite_tile_id,
            "started_at": (started_at or now).isoformat(),
            "completed_at": (
                now.isoformat() if step_status in ("completed", "failed") else None
            ),
            "duration_ms": (
                int((now - started_at).total_seconds() * 1000)
                if started_at
                else None
            ),
            "error_message": error_message,
            "error_details": error_details,
            "metadata": metadata or {},
        }

        # Remove NULL fields to keep inserts clean
        payload = {k: v for k, v in payload.items() if v is not None}

        supabase.table("ndvi_processing_logs").insert(payload).execute()
        
        logger.debug(
            f"NDVI log inserted | step={processing_step} | "
            f"status={step_status} | land={land_id}"
        )

    except Exception as e:
        # Detailed error logging without breaking pipeline
        logger.warning(
            f"NDVI log insert failed | step={processing_step} | "
            f"land={land_id} | error={str(e)}"
        )
        
        # If this is a critical error (table doesn't exist), log once
        if "relation" in str(e).lower() and "does not exist" in str(e).lower():
            logger.error(
                "CRITICAL: ndvi_processing_logs table does not exist. "
                "Please create it in Supabase. Processing will continue "
                "but logs will not be saved."
            )


# --------------------------------------------------
# Get Supabase client (for passing to processor)
# --------------------------------------------------
def get_supabase_client() -> Client:
    """
    Return the configured Supabase client.
    Used by processor to upload files to storage.
    """
    return supabase
