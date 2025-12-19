import os
from typing import List, Dict, Optional
from supabase import create_client, Client
from logger import logger
from dotenv import load_dotenv
load_dotenv()
from datetime import date, datetime, UTC



# --------------------------------------------------
# Supabase Client
# --------------------------------------------------
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

if not SUPABASE_URL or not SUPABASE_KEY:
    raise RuntimeError("Supabase credentials not set")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)


# --------------------------------------------------
# Fetch active lands
# --------------------------------------------------
def fetch_lands(limit: int = 100) -> List[Dict]:
    try:
        res = (
            supabase.table("lands")
            .select("id, tenant_id, area_guntas, current_crop, boundary_polygon_old")
            .eq("is_active", True)
            .limit(limit)
            .execute()
        )
        return res.data or []

    except Exception as e:
        logger.exception("Failed to fetch lands")
        return []


# --------------------------------------------------
# Insert / upsert NDVI data (IDEMPOTENT)
# --------------------------------------------------
def insert_ndvi(row: Dict) -> None:
    """
    Upserts NDVI row using (land_id, date) uniqueness.
    Safe for re-runs.
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

    except Exception as e:
        logger.exception(
            f"NDVI insert failed for land {row.get('land_id')}"
        )
        raise


# --------------------------------------------------
# Update lands table (latest NDVI snapshot)
# --------------------------------------------------
def update_land(
    land_id: str,
    ndvi: float,
    thumbnail_url: Optional[str],
) -> None:
    try:
        (
            supabase.table("lands")
            .update(
                {
                    "ndvi": round(ndvi, 3),
                    "ndvi_thumbnail_url": thumbnail_url,
                    "ndvi_updated_at": datetime.now(UTC).isoformat()
                }
            )
            .eq("id", land_id)
            .execute()
        )

    except Exception as e:
        logger.exception(f"Failed to update land {land_id}")
        raise


# --------------------------------------------------
# Processing logs (observability)
# --------------------------------------------------
def log_step(
    step: str,
    status: str,
    land_id: str,
    tenant_id: str,
    error: Optional[str] = None,
) -> None:
    try:
        supabase.table("ndvi_processing_logs").insert(
            {
                "step": step,
                "status": status,
                "land_id": land_id,
                "tenant_id": tenant_id,
                "error": error,
            }
        ).execute()

    except Exception:
        # Never break pipeline due to logging failure
        logger.warning(
            f"Log insert failed for land {land_id}, step {step}"
        )
