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
             # boundary_geom is the PostGIS column and is populated on 29 of
             # 29 active lands, but was never selected - so every observation
             # in the 2026-08-07 run carried geometry_confidence='medium',
             # silently falling back to the legacy jsonb. PostgREST returns
             # geometry as GeoJSON, which shapely.shape() consumes directly.
             .select("id, tenant_id, area_acres, area_guntas, current_crop, "
                     "current_crop_id, boundary_geom, boundary_polygon_old, "
                     "mgrs_tile_id, tile_id, center_lat, center_lon, "
                     "crop_cycle, transplant_date, planting_date, "
                     "last_sowing_date, das")
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
_KNOWN_COLUMNS = None          # cache; None = not probed yet
_DROPPED_REPORTED = set()


def known_columns() -> Optional[set]:
    """
    Columns that actually exist on public.ndvi_data, probed once from a real
    row. Returns None if the probe fails or the table is empty (then nothing
    is filtered).

    WHY THIS EXISTS. v3 writes evidence fields (effective_pixel_count,
    coverage_weighted_purity, ...) that do not exist until the accompanying
    migration is applied. PostgREST rejects the WHOLE batch on an unknown
    column, so without this probe deploying the code before the migration
    would fail every land, every night. Everything filtered out here is
    still persisted inside metadata.evidence, so no evidence is lost - only
    its promotion to a first-class column waits for the migration.
    """
    global _KNOWN_COLUMNS
    if _KNOWN_COLUMNS is not None:
        return _KNOWN_COLUMNS or None
    try:
        res = supabase.table("ndvi_data").select("*").limit(1).execute()
        if res.data:
            _KNOWN_COLUMNS = set(res.data[0].keys())
            logger.info(f"ndvi_data schema probe: {len(_KNOWN_COLUMNS)} columns")
        else:
            _KNOWN_COLUMNS = set()
    except Exception as e:
        logger.warning(f"schema probe failed ({e}); writing rows unfiltered")
        _KNOWN_COLUMNS = set()
    return _KNOWN_COLUMNS or None


def _filter_to_schema(rows: List[Dict]) -> List[Dict]:
    cols = known_columns()
    if not cols:
        return rows
    out = []
    for r in rows:
        extra = set(r) - cols
        if extra:
            for k in sorted(extra):
                if k not in _DROPPED_REPORTED:
                    _DROPPED_REPORTED.add(k)
                    logger.warning(
                        f"column '{k}' absent from ndvi_data - kept in "
                        f"metadata.evidence only. Apply the v3 migration to "
                        f"promote it to a column.")
            r = {k: v for k, v in r.items() if k in cols}
        out.append(r)
    return out


def upsert_observations(rows: List[Dict], run_started_at: datetime = None):
    """
    Conflict target is (land_id, scene_id). Returns (upserted, newly_inserted).

    v2.1 returned len(rows) and main.py reported it as "observations
    written": on 2026-08-29 the run reported 30 written while 0 rows were
    created (F-6). newly_inserted is measured from created_at >= run start,
    so the run summary and the exit-code guard see REAL new data.
    """
    if not rows:
        return 0, 0
    land_id = rows[0].get("land_id")
    rows = _filter_to_schema(rows)
    try:
        with_retry(
            lambda: supabase.table("ndvi_data")
                    .upsert(rows, on_conflict="land_id,scene_id").execute(),
            what=f"upsert {len(rows)} observation(s) for land {land_id}",
        )
    except Exception:
        logger.exception(f"upsert failed for {len(rows)} rows (land {land_id})")
        raise

    new = 0
    if run_started_at is not None:
        try:
            scene_ids = [r["scene_id"] for r in rows if r.get("scene_id")]
            res = with_retry(
                lambda: supabase.table("ndvi_data").select("id", count="exact")
                   .eq("land_id", land_id).in_("scene_id", scene_ids)
                   .gte("created_at", run_started_at.isoformat())
                   .limit(1).execute(),
                what=f"new-row count {land_id}", attempts=3)
            new = int(res.count or 0)
        except Exception as e:
            logger.warning(f"new-row count failed for {land_id}: {e}")
            new = -1   # unknown; caller treats as "could not verify"
    return len(rows), new


def optical_history(land_id: str, days: int) -> List[Dict]:
    """Stored optical observations in the last `days` (for temporal checks)."""
    from datetime import date, timedelta
    since = (date.today() - timedelta(days=days)).isoformat()
    try:
        res = with_retry(
            lambda: supabase.table("ndvi_data")
                .select("acquisition_date, ndvi_value, scene_id")
                .eq("land_id", land_id)
                .eq("observation_source", "sentinel-2")
                .eq("observation_type", "observed")
                .not_.is_("ndvi_value", "null")
                .gte("acquisition_date", since)
                .order("acquisition_date", desc=True)
                .limit(30).execute(),
            what=f"optical_history {land_id}", attempts=3)
        return res.data or []
    except Exception as e:
        logger.warning(f"optical_history failed for {land_id}: {type(e).__name__}: {e}")
        return []


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
    if ndvi_value is None:
        # F-3: the radar path used to write last_ndvi_value = NULL over a
        # valid optical cache. The cache is OPTICAL ONLY; radar-only runs
        # must go through mark_land_status() instead.
        raise ValueError("update_land_snapshot requires an optical ndvi_value; "
                         "use mark_land_status for radar-only runs")
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
        with_retry(
            lambda: supabase.table("lands").update({
                "ndvi_status": status,
                "ndvi_status_note": note,
                "last_processed_at": datetime.now(timezone.utc).isoformat(),
            }).eq("id", land_id).execute(),
            what=f"land status update {land_id}", attempts=2,
        )
    except Exception as e:
        logger.warning(f"status update failed for {land_id}: {type(e).__name__}: {e}")


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
        with_retry(lambda: supabase.table("ndvi_run_summary").insert(summary).execute(),
                   what="run summary insert", attempts=2)
    except Exception as e:
        logger.warning(f"run summary insert failed (table may not exist yet): {e}")

# ---------------------------------------------------------------------------
# SCENE EVALUATION LEDGER (sql/2026-09-23_ndvi_scene_evaluations.sql)
# ---------------------------------------------------------------------------
# Lets the nightly run skip (land, scene) pairs it has already measured. The
# pipeline must behave EXACTLY as before when the table does not exist yet, so
# every function here degrades to "nothing is known" on any error.
_LEDGER_AVAILABLE = True
_LEDGER_CHUNK = 100          # land ids per request: keeps the URL well short


def fetch_scene_ledger(land_ids: List[str], pipeline_version: str,
                       since_iso: str) -> Optional[Dict[str, Dict[str, list]]]:
    """{land_id: {scene_id: [(geometry_fingerprint, outcome), ...]}} or None.

    One request per 100 lands - a whole read-block in one or two calls, never
    one call per land. None means "ledger unavailable": callers then evaluate
    every scene, which is the pre-ledger behaviour.
    """
    global _LEDGER_AVAILABLE
    if not _LEDGER_AVAILABLE or not land_ids:
        return None
    out: Dict[str, Dict[str, list]] = {}
    try:
        for i in range(0, len(land_ids), _LEDGER_CHUNK):
            chunk = land_ids[i:i + _LEDGER_CHUNK]
            res = with_retry(
                lambda: supabase.table("ndvi_scene_evaluations")
                    .select("land_id, scene_id, geometry_fingerprint, outcome")
                    .in_("land_id", chunk)
                    .eq("pipeline_version", pipeline_version)
                    .gte("acquisition_date", since_iso)
                    .limit(10000).execute(),
                what=f"scene ledger for {len(chunk)} land(s)", attempts=3)
            for r in res.data or []:
                out.setdefault(r["land_id"], {}).setdefault(r["scene_id"], []).append(
                    (r["geometry_fingerprint"], r["outcome"]))
        return out
    except Exception as e:
        text = f"{type(e).__name__}: {e}"
        if "ndvi_scene_evaluations" in text or "42P01" in text or "PGRST205" in text:
            _LEDGER_AVAILABLE = False
            logger.warning("scene ledger table not present - evaluating every scene "
                           "(apply sql/2026-09-23_ndvi_scene_evaluations.sql to enable skipping)")
        else:
            logger.warning(f"scene ledger read failed ({text}) - evaluating every scene this block")
        return None


def record_scene_evaluations(rows: List[Dict]) -> int:
    """Batched upsert of deterministic outcomes. Never raises."""
    if not _LEDGER_AVAILABLE or not rows:
        return 0
    try:
        with_retry(
            lambda: supabase.table("ndvi_scene_evaluations")
                .upsert(rows, on_conflict="land_id,scene_id,pipeline_version,geometry_fingerprint")
                .execute(),
            what=f"record {len(rows)} scene evaluation(s)", attempts=3)
        return len(rows)
    except Exception as e:
        logger.warning(f"scene ledger write failed for {len(rows)} row(s): {type(e).__name__}: {e} "
                       "- those scenes will simply be re-evaluated next run")
        return 0


# ---------------------------------------------------------------------------
# BATCHED WRITES (one request per chunk of a read-block, not per land)
# ---------------------------------------------------------------------------
# Every function keeps per-land ISOLATION: if a batch fails, it is retried
# land by land so one bad row cannot fail its neighbours, and the caller learns
# exactly which lands did and did not persist.
_BATCH_ROWS = 500


def build_log_payload(*, processing_step: str, step_status: str, tenant_id: str = None,
                      land_id: str = None, started_at: datetime = None,
                      error_message: str = None, metadata: Dict = None) -> Dict:
    """The exact row log_step would insert, without inserting it."""
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
    return {k: v for k, v in payload.items() if v is not None}


def log_steps_batch(payloads: List[Dict]) -> int:
    """Insert many processing-log rows. Never raises (logs are diagnostic)."""
    done = 0
    for i in range(0, len(payloads), _BATCH_ROWS):
        chunk = payloads[i:i + _BATCH_ROWS]
        try:
            with_retry(lambda: supabase.table("ndvi_processing_logs").insert(chunk).execute(),
                       what=f"log batch insert ({len(chunk)})", attempts=2)
            done += len(chunk)
        except Exception as e:
            logger.warning(f"log batch insert failed ({len(chunk)} rows): {type(e).__name__}: {e}")
    return done


def optical_history_batch(land_ids: List[str], days: int) -> Dict[str, List[Dict]]:
    """optical_history for many lands in one request per 100 lands."""
    from datetime import date, timedelta
    since = (date.today() - timedelta(days=days)).isoformat()
    out: Dict[str, List[Dict]] = {lid: [] for lid in land_ids}
    for i in range(0, len(land_ids), 100):
        chunk = land_ids[i:i + 100]
        try:
            res = with_retry(
                lambda: supabase.table("ndvi_data")
                    .select("land_id, acquisition_date, ndvi_value, scene_id")
                    .in_("land_id", chunk)
                    .eq("observation_source", "sentinel-2")
                    .eq("observation_type", "observed")
                    .not_.is_("ndvi_value", "null")
                    .gte("acquisition_date", since)
                    .order("acquisition_date", desc=True)
                    .limit(30 * len(chunk)).execute(),
                what=f"optical_history batch ({len(chunk)} lands)", attempts=3)
            for r in res.data or []:
                lst = out.setdefault(r["land_id"], [])
                if len(lst) < 30:                       # same per-land cap as optical_history
                    lst.append({k: r[k] for k in ("acquisition_date", "ndvi_value", "scene_id")})
        except Exception as e:
            logger.warning(f"optical_history batch failed ({type(e).__name__}: {e}); per-land fallback")
            for lid in chunk:
                out[lid] = optical_history(lid, days)
    return out


def upsert_observations_batch(rows: List[Dict], run_started_at: datetime = None):
    """Upsert observations for many lands. Returns (written, new, failed_lands).

    written / new: {land_id: count}. new is measured from created_at >= run
    start (as upsert_observations does), -1 when it could not be verified.
    failed_lands: lands whose rows could not be stored even individually."""
    by_land: Dict[str, List[Dict]] = {}
    for r in rows:
        by_land.setdefault(r["land_id"], []).append(r)
    written: Dict[str, int] = {}
    failed: set = set()
    clean = _filter_to_schema(rows)
    for i in range(0, len(clean), _BATCH_ROWS):
        chunk = clean[i:i + _BATCH_ROWS]
        lands_in = {r["land_id"] for r in chunk}
        try:
            with_retry(lambda: supabase.table("ndvi_data")
                       .upsert(chunk, on_conflict="land_id,scene_id").execute(),
                       what=f"observation batch upsert ({len(chunk)})")
            for r in chunk:
                written[r["land_id"]] = written.get(r["land_id"], 0) + 1
        except Exception as e:
            logger.warning(f"observation batch failed ({type(e).__name__}); isolating per land")
            for lid in lands_in:
                try:
                    n, _ = upsert_observations(by_land[lid])
                    written[lid] = n
                except Exception:
                    failed.add(lid)
    new: Dict[str, int] = {}
    ok = [lid for lid in written if lid not in failed]
    if run_started_at is not None and ok:
        for i in range(0, len(ok), 100):
            chunk = ok[i:i + 100]
            sids = list({r["scene_id"] for lid in chunk for r in by_land[lid] if r.get("scene_id")})
            try:
                res = with_retry(
                    lambda: supabase.table("ndvi_data").select("land_id")
                        .in_("land_id", chunk).in_("scene_id", sids)
                        .gte("created_at", run_started_at.isoformat())
                        .limit(10000).execute(),
                    what=f"new-row count batch ({len(chunk)})", attempts=3)
                for lid in chunk:
                    new[lid] = 0
                for r in res.data or []:
                    new[r["land_id"]] = new.get(r["land_id"], 0) + 1
            except Exception as e:
                logger.warning(f"new-row count batch failed: {e}")
                for lid in chunk:
                    new[lid] = -1
    return written, new, failed



# ---------------------------------------------------------------------------
# STREAMING LAND ITERATION (two-pass: light keys first, geometry per group)
# ---------------------------------------------------------------------------
# iter_lands() yields full rows including boundary geometry, and main grouped
# ALL of them in memory before starting. At 1M lands that is gigabytes of
# GeoJSON held at once. Pass 1 streams only the columns grouping needs; pass 2
# fetches full rows one group at a time. Pagination is KEYSET (id > last id),
# which stays O(page) at any depth, unlike OFFSET.
_LAND_FULL_COLUMNS = ("id, tenant_id, area_acres, area_guntas, current_crop, "
                      "current_crop_id, boundary_geom, boundary_polygon_old, "
                      "mgrs_tile_id, tile_id, center_lat, center_lon, "
                      "crop_cycle, transplant_date, planting_date, "
                      "last_sowing_date, das")


def iter_land_keys(tenant_id: Optional[str] = None, page_size: int = 1000) -> Iterator[Dict]:
    """Every eligible land's grouping keys only, keyset-paginated by id."""
    last = None
    while True:
        q = (supabase.table("lands")
             .select("id, tenant_id, tile_id, mgrs_tile_id, center_lat, center_lon")
             .eq("is_active", True).is_("deleted_at", None)
             .order("id").limit(page_size))
        if tenant_id:
            q = q.eq("tenant_id", tenant_id)
        if last is not None:
            q = q.gt("id", last)
        try:
            rows = with_retry(lambda: q.execute(), what="land keys page", attempts=3).data or []
        except Exception:
            logger.exception(f"land keys page failed after id {last}")
            return
        if not rows:
            return
        for r in rows:
            yield r
        last = rows[-1]["id"]
        if len(rows) < page_size:
            return


def fetch_lands_by_ids(ids: List[str]) -> List[Dict]:
    """Full land rows (geometry included) for one group, 100 ids per request.
    Re-applies the eligibility filter, so a land deactivated between pass 1
    and pass 2 is not processed."""
    out: List[Dict] = []
    for i in range(0, len(ids), 100):
        chunk = ids[i:i + 100]
        try:
            res = with_retry(
                lambda: supabase.table("lands").select(_LAND_FULL_COLUMNS)
                    .in_("id", chunk).eq("is_active", True).is_("deleted_at", None)
                    .order("id").execute(),
                what=f"land rows ({len(chunk)})", attempts=3)
            out.extend(res.data or [])
        except Exception:
            logger.exception(f"fetch_lands_by_ids failed for {len(chunk)} id(s)")
    return out
