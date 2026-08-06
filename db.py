"""
db.py - Supabase access layer.

v1 DEFECT FIXED (P-08):
    def fetch_lands(limit: int = 100)
        .limit(limit)            # hard cap, no ORDER BY, no pagination
    main.py called fetch_lands() with no argument -> always 100.
    There was no code path by which land #101 was ever processed. At
    100,000 farms that is 0.1% coverage, reported as success.
    Confirmed live in the 2026-08-06 Actions log:
      GET .../lands?...&deleted_at=is.null&limit=100

v1 DEFECT FIXED (P-18): the pipeline read boundary_polygon_old (legacy jsonb)
    while the platform maintains a PostGIS boundary_geom. Two boundary
    sources with no synchronisation guarantee. v2 prefers boundary_geom.
"""

import os
from typing import List, Dict, Optional, Iterator
from datetime import datetime, timezone

from supabase import create_client, Client
from dotenv import load_dotenv

from config import LAND_PAGE_SIZE, MAX_LANDS_PER_RUN
from logger import logger

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

if not SUPABASE_URL or not SUPABASE_KEY:
    raise RuntimeError("SUPABASE_URL and SUPABASE_KEY must be set")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)


def get_supabase_client() -> Client:
    return supabase


# ---------------------------------------------------------------------------
# TRANSIENT-ERROR RETRY
# ---------------------------------------------------------------------------
# The first successful production run lost several writes to:
#     httpx.RemoteProtocolError: Server disconnected
# PostgREST over HTTP/2 drops idle multiplexed streams under concurrency
# (TILE_WORKERS=4). These are transient and safely retryable: every write in
# this pipeline is either an idempotent upsert keyed on (land_id, scene_id)
# or a last-writer-wins snapshot update.
#
# Without this, a dropped connection silently skipped a lands snapshot update
# - the same class of silent loss the whole audit was about.
_TRANSIENT = (
    "server disconnected", "connection reset", "connection aborted",
    "remoteprotocolerror", "timeout", "temporarily unavailable",
    "connection error", "read timeout", "502", "503", "504",
)


def _is_transient(exc: Exception) -> bool:
    msg = f"{type(exc).__name__} {exc}".lower()
    return any(t in msg for t in _TRANSIENT)


def with_retry(fn, *, what: str, attempts: int = 3, base_delay: float = 0.5):
    """
    Run fn(), retrying transient network failures with exponential backoff.

    Non-transient errors (constraint violations, missing columns) raise
    immediately - retrying those would only hide a real defect.
    """
    import time as _time
    last = None
    for i in range(attempts):
        try:
            return fn()
        except Exception as e:
            last = e
            if not _is_transient(e):
                raise
            if i == attempts - 1:
                break
            delay = base_delay * (2 ** i)
            logger.warning(
                f"{what}: transient failure ({type(e).__name__}), "
                f"retry {i+1}/{attempts-1} in {delay:.1f}s"
            )
            _time.sleep(delay)
    logger.error(f"{what}: FAILED after {attempts} attempts: {last}")
    raise last


# ---------------------------------------------------------------------------
# LANDS - paginated, ordered, complete
# ---------------------------------------------------------------------------
def iter_lands(tenant_id: Optional[str] = None) -> Iterator[Dict]:
    """
    Yield EVERY eligible land, page by page, in a stable order.

    Stable ORDER BY id is what makes pagination correct: without it
    PostgREST may return overlapping or missing rows between pages.
    """
    offset, yielded = 0, 0

    while True:
        q = (supabase.table("lands")
             .select("id, tenant_id, area_acres, area_guntas, current_crop, "
                     "current_crop_id, boundary_polygon_old, mgrs_tile_id, "
                     "tile_id, center_lat, center_lon, crop_cycle, "
                     "transplant_date, planting_date, last_sowing_date, das")
             .eq("is_active", True)
             .is_("deleted_at", None)
             .order("id")
             .range(offset, offset + LAND_PAGE_SIZE - 1))

        if tenant_id:
            q = q.eq("tenant_id", tenant_id)

        try:
            rows = q.execute().data or []
        except Exception:
            logger.exception(f"fetch_lands page failed at offset {offset}")
            return

        if not rows:
            return

        for r in rows:
            yield r
            yielded += 1
            if MAX_LANDS_PER_RUN and yielded >= MAX_LANDS_PER_RUN:
                logger.warning(f"MAX_LANDS_PER_RUN={MAX_LANDS_PER_RUN} reached")
                return

        if len(rows) < LAND_PAGE_SIZE:
            return
        offset += LAND_PAGE_SIZE


def count_eligible_lands(tenant_id: Optional[str] = None) -> int:
    q = (supabase.table("lands").select("id", count="exact")
         .eq("is_active", True).is_("deleted_at", None).limit(1))
    if tenant_id:
        q = q.eq("tenant_id", tenant_id)
    return q.execute().count or 0


# ---------------------------------------------------------------------------
# NDVI WRITE - idempotent on TRUE acquisition identity
# ---------------------------------------------------------------------------
def upsert_observations(rows: List[Dict]) -> int:
    """
    Conflict target is (land_id, scene_id), not (land_id, date).

    v1 used (land_id, date) where date was the pipeline RUN date, so every
    daily run minted a new key for the same underlying scene - producing
    96.4% consecutive-day spacing and 72.2% exact repeated values from a
    satellite that revisits every ~3 days.

    Keying on scene_id makes re-runs and backfills genuinely idempotent:
    the same acquisition can never be stored twice under different dates.
    """
    if not rows:
        return 0
    try:
        with_retry(
            lambda: supabase.table("ndvi_data")
                    .upsert(rows, on_conflict="land_id,scene_id").execute(),
            what=f"upsert {len(rows)} observation(s) for land {rows[0].get('land_id')}",
        )
        return len(rows)
    except Exception:
        logger.exception(f"upsert failed for {len(rows)} rows "
                         f"(land {rows[0].get('land_id')})")
        raise


def latest_observation(land_id: str) -> Optional[Dict]:
    try:
        r = (supabase.table("ndvi_data")
             .select("acquisition_date, ndvi_value, quality_score, "
                     "observation_source, scene_id")
             .eq("land_id", land_id)
             .eq("observation_type", "observed")
             .not_.is_("ndvi_value", "null")
             .order("acquisition_date", desc=True)
             .limit(1).execute().data)
        return r[0] if r else None
    except Exception:
        logger.exception(f"latest_observation failed for {land_id}")
        return None


def update_land_snapshot(*, land_id: str, ndvi_value, acquisition_date,
                         quality_score, source: str,
                         thumbnail_url=None, geotiff_url=None) -> None:
    """
    Denormalised cache in `lands`.

    v1 wrote last_ndvi_calculation = date.today() rather than the
    acquisition date, which is why 24 of 34 lands (70.6%) had a cache date
    that disagreed with ndvi_data, with value divergence up to 0.360.
    v2 writes the TRUE acquisition date and the quality that produced it.
    """
    data = {
        "last_ndvi_value": ndvi_value,
        "last_ndvi_calculation": acquisition_date,   # TRUE acquisition date
        "last_ndvi_quality": quality_score,
        "last_ndvi_source": source,
        "ndvi_tested": True,
        "ndvi_status": "completed",
        "last_processed_at": datetime.now(timezone.utc).isoformat(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    if thumbnail_url:
        data["ndvi_thumbnail_url"] = thumbnail_url
    if geotiff_url:
        data["ndvi_geotiff_url"] = geotiff_url

    try:
        with_retry(
            lambda: supabase.table("lands").update(data).eq("id", land_id).execute(),
            what=f"lands snapshot update {land_id}",
        )
    except Exception as e:
        # Log the message, not a 60-line HTTP traceback. The traceback told us
        # nothing the message doesn't, and it buried the actual run outcome.
        logger.error(f"snapshot update failed for {land_id}: {type(e).__name__}: {e}")


def mark_land_status(land_id: str, status: str, note: str = None) -> None:
    try:
        supabase.table("lands").update({
            "ndvi_status": status,
            "ndvi_status_note": note,
            "last_processed_at": datetime.now(timezone.utc).isoformat(),
        }).eq("id", land_id).execute()
    except Exception:
        logger.warning(f"status update failed for {land_id}")


# ---------------------------------------------------------------------------
# OBSERVABILITY
# ---------------------------------------------------------------------------
def log_step(*, processing_step: str, step_status: str, tenant_id: str = None,
             land_id: str = None, started_at: datetime = None,
             error_message: str = None, metadata: Dict = None) -> None:
    try:
        now = datetime.now(timezone.utc)
        payload = {
            "processing_step": processing_step,
            "step_status": step_status,
            "tenant_id": tenant_id,
            "land_id": land_id,
            "started_at": (started_at or now).isoformat(),
            "completed_at": now.isoformat() if step_status in ("completed", "failed", "skipped") else None,
            "duration_ms": int((now - started_at).total_seconds() * 1000) if started_at else None,
            "error_message": error_message,
            "metadata": metadata or {},
        }
        with_retry(
            lambda: supabase.table("ndvi_processing_logs")
                    .insert({k: v for k, v in payload.items() if v is not None}).execute(),
            what=f"log insert [{processing_step}]", attempts=2,
        )
    except Exception as e:
        logger.warning(f"log insert failed [{processing_step}]: {e}")


def write_run_summary(summary: Dict) -> None:
    """
    A RUN-LEVEL health record.

    This is the observability gap that let a 29-day total outage pass
    unnoticed: GitHub Actions reported 'succeeded' every night because the
    process exited 0, and pg_cron reported success because the HTTP POST was
    queued. Nothing measured whether any DATA was produced.

    main.py exits NON-ZERO when the observation count is zero, so the
    scheduler goes red on a silent failure.
    """
    try:
        supabase.table("ndvi_run_summary").insert(summary).execute()
    except Exception as e:
        logger.warning(f"run summary insert failed (table may not exist yet): {e}")
