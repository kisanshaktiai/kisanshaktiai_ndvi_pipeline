"""NDVI pipeline entrypoint with separate observed parcel-context, water evidence, and intelligence persistence."""

import sys
import argparse
from datetime import datetime, timezone, date
from concurrent.futures import ThreadPoolExecutor, as_completed, wait, FIRST_COMPLETED
from typing import List

from db import (
    iter_lands, count_eligible_lands, upsert_observations, optical_history,
    iter_land_keys, fetch_lands_by_ids,
    update_land_snapshot, mark_land_status, log_step, write_run_summary,
    fetch_scene_ledger, record_scene_evaluations,
    build_log_payload, log_steps_batch, optical_history_batch, upsert_observations_batch,
    upsert_water_layers_batch, apply_land_updates_batch, update_land_snapshot_thumbnail,
)
from processor import PIPELINE_VERSION, S2_BANDS_10M, S2_BANDS_20M
from water_layers import WATER_LAYER_VERSION
from parcel_context import CONTEXT_VERSION
from zone_stats import ZONES_VERSION
from raster_io import RENDER_VERSION
from ndvi_intelligence_writer import INTELLIGENCE_VERSION

# The scene ledger's identity is EVERY output this pipeline produces, not only
# the measurement. If only the water algorithm (or the context ring, the zone
# rules, the images, the intelligence record) changes, the ledger must not say
# "already processed" - so each output's version is part of the key. Bumping
# any one of them re-evaluates every scene once, under the new rules.
PROCESSING_IDENTITY = "+".join([
    f"measure:{PIPELINE_VERSION}", f"water:{WATER_LAYER_VERSION}", f"context:{CONTEXT_VERSION}",
    f"zones:{ZONES_VERSION}", f"render:{RENDER_VERSION}", f"intel:{INTELLIGENCE_VERSION}",
])
from context_enrichment import process_land_with_context
from water_layer_runner import process_land_water_layers
from ndvi_intelligence_writer import persist_observed_intelligence, persist_observed_intelligence_batch
from tile_grouping import group_lands_by_tile, scenes_for_group, log_group_plan
from tile_reader import (read_scene_block, plan_blocks, release_block, scenes_for_block,
                         geometry_fingerprint, CONTEXT_BLOCK_PAD_M)
from resource_budget import RASTER_CACHE, COUNTERS
from raster_utils import resolve_geometry
from config import BLOCK_SPAN_M, TILE_BATCH_READS, TILE_WORKERS, BLOCK_MAX_LANDS, INCREMENTAL_SCENES, IMAGE_UPLOAD_WORKERS
from phenology import classify_trend
from config import TILE_WORKERS, LOOKBACK_DAYS, BACKFILL_DAYS, TEMPORAL_LOOKBACK_DAYS
from logger import logger


def _new_result(land_id: str) -> dict:
    return {"land_id": land_id, "rows": 0, "new_rows": 0, "source": None,
            "status": "skipped", "error": None, "context_enriched": 0,
            "context_failed": 0, "water_layers": 0, "intelligence_written": 0,
            "intelligence_failed": 0, "scene_evaluations": [], "scenes_skipped_known": 0}


def _evaluations(land_id, tenant_id, report, accepted_rows):
    """Deterministic outcomes for the scene ledger. Transient errors
    (report['scene_errors']) are deliberately NOT recorded, so they retry."""
    fingerprint = (report or {}).get("geometry_fingerprint")
    if not fingerprint:
        return []
    base = {"land_id": land_id, "tenant_id": tenant_id,
            "pipeline_version": PROCESSING_IDENTITY, "geometry_fingerprint": fingerprint}
    out = [{**base, "scene_id": r["scene_id"], "outcome": "rejected",
            "reason": (r.get("reason") or "")[:200], "acquisition_date": r.get("acquisition_date")}
           for r in report.get("optical_rejects", []) if r.get("scene_id")]
    out += [{**base, "scene_id": r["scene_id"], "outcome": "rejected",
             "reason": (r.get("reason") or "")[:200], "acquisition_date": r.get("acquisition_date")}
            for r in report.get("s1_rejects", []) if r.get("scene_id")]
    out += [{**base, "scene_id": r["scene_id"], "outcome": "accepted", "reason": None,
             "acquisition_date": r.get("acquisition_date")}
            for r in accepted_rows
            if r.get("observation_source") in ("sentinel-2", "sentinel-1") and r.get("scene_id")]
    return out


def measure_land(land: dict, lookback: int, scenes=None, blocks=None, ledger=None,
                 history=None, s1_search=None, s1_block_for=None) -> dict:
    """PHASE 1 - read and compute one land. Writes nothing to ndvi_data,
    ndvi_intelligence, lands or the ledger; its processing logs are BUFFERED.
    (Water layers still write their own rows here, unchanged.)"""
    land_id, tenant_id = land["id"], land["tenant_id"]
    started = datetime.now(timezone.utc)
    pend = {"land": land, "started": started, "result": _new_result(land_id),
            "logs": [build_log_payload(processing_step="PROCESS_START", step_status="started",
                                       tenant_id=tenant_id, land_id=land_id, started_at=started)],
            "history": history if history is not None else [], "rows": [], "report": {},
            "context_report": {}, "error": None,
            "water_records": [], "uploads": []}
    try:
        if history is None:
            pend["history"] = optical_history(land_id, TEMPORAL_LOOKBACK_DAYS)
        rows, report = process_land_with_context(
            land, lookback_days=lookback, scenes=scenes, history=pend["history"],
            blocks=blocks, ledger=ledger, s1_search=s1_search, s1_block_for=s1_block_for,
        )
        pend["rows"], pend["report"] = rows, report
        pend["result"]["scenes_skipped_known"] = int(report.get("scenes_skipped_known") or 0)
        ctx = report.get("parcel_context") or {}
        pend["context_report"] = ctx
        pend["result"]["context_enriched"] = int(ctx.get("rows_enriched") or 0)
        pend["result"]["context_failed"] = int(ctx.get("rows_failed") or 0)
        pend["result"]["context_not_applicable"] = int(ctx.get("rows_not_applicable") or 0)

        # Water layers are observational evidence and are intentionally
        # generated independently of NDVI acceptance. No water-stress
        # threshold or agronomic recommendation is made here.
        try:
            pend["result"]["water_layers"] = process_land_water_layers(
                land, scenes=scenes, lookback_days=lookback,
                scene_bands=report.get("scene_bands"),
                only_scene_ids=report.get("pending_scene_ids") if ledger else None,
                sink=pend["water_records"], uploads=pend["uploads"],
            )
        except Exception as water_exc:
            logger.warning(f"Water-layer pass failed for land {land_id}: {type(water_exc).__name__}: {water_exc}")
            pend["logs"].append(build_log_payload(
                processing_step="WATER_LAYER_ERROR", step_status="failed", tenant_id=tenant_id,
                land_id=land_id, started_at=started, error_message=str(water_exc)[:1000]))
        finally:
            cached = report.pop("scene_bands", None) or {}
            for entry in cached.values():        # return bytes to the process-wide budget
                RASTER_CACHE.release(int(entry.get("_nbytes") or 0))

        for err in report.get("scene_errors", []):
            pend["logs"].append(build_log_payload(
                processing_step="SCENE_ERROR", step_status="failed", tenant_id=tenant_id,
                land_id=land_id, started_at=started, error_message=str(err.get("error"))[:1000],
                metadata={k: v for k, v in err.items() if k != "error"}))
    except Exception as e:
        logger.exception(f"Land {land_id} failed")
        pend["error"] = e
    return pend


class _LandUpdates:
    """Collects a block's land snapshot/status changes as ONE entry per land
    and writes them with db.apply_land_updates_batch (release review FIX-1:
    one request per block instead of one per land). Later calls for the same
    land override earlier ones field by field, exactly as sequential updates
    would. If the batch function is unavailable, it falls back to the original
    per-land update_land_snapshot / mark_land_status calls."""

    def __init__(self):
        self._e = {}

    def _entry(self, land_id):
        return self._e.setdefault(land_id, {"id": land_id})

    def snapshot(self, *, land_id, ndvi_value, acquisition_date, quality_score, source,
                 thumbnail_url=None, geotiff_url=None):
        if ndvi_value is None:
            raise ValueError("snapshot requires an optical ndvi_value; use status for radar-only runs")
        e = self._entry(land_id)
        e.update(has_snapshot=True, last_ndvi_value=ndvi_value, last_ndvi_calculation=acquisition_date,
                 last_ndvi_quality=quality_score, last_ndvi_source=source, ndvi_status="completed")
        if thumbnail_url:
            e["ndvi_thumbnail_url"] = thumbnail_url

    def status(self, land_id, status, note=None):
        e = self._entry(land_id)
        e.update(ndvi_status=status, set_note=True, ndvi_status_note=note)

    def thumbnail(self, land_id, path):
        self._entry(land_id)["ndvi_thumbnail_url"] = path

    def flush(self):
        entries = list(self._e.values())
        self._e = {}
        if not entries:
            return
        if apply_land_updates_batch(entries) is not None:
            COUNTERS.bump("land_update_batches")
            return
        for e in entries:                               # fallback: original per-land writes
            if e.get("has_snapshot"):
                update_land_snapshot(land_id=e["id"], ndvi_value=e["last_ndvi_value"],
                                     acquisition_date=e["last_ndvi_calculation"],
                                     quality_score=e["last_ndvi_quality"], source=e["last_ndvi_source"],
                                     thumbnail_url=e.get("ndvi_thumbnail_url"))
            elif e.get("ndvi_thumbnail_url"):
                update_land_snapshot_thumbnail(e["id"], e["ndvi_thumbnail_url"])
            if e.get("set_note"):
                mark_land_status(e["id"], e["ndvi_status"], e.get("ndvi_status_note"))


def persist_block(pendings: List[dict], lookback: int, run_started: datetime) -> List[dict]:
    """PHASE 2 - every write for a block, batched, in the order that keeps the
    stored state honest:
      1. observations (one batch; per-land isolation if it fails)
      2. observed intelligence, only for lands whose observations stored
      3. land snapshot/status, forward-only, only for lands that changed
      4. ledger outcomes (returned to the caller; accepted ONLY if stored)
      5. processing logs (one batch)
    """
    results, logs, to_store = [], [], []
    lu = _LandUpdates()          # one merged entry per land, written in one batch
    for p in pendings:
        land_id, tenant_id = p["land"]["id"], p["land"]["tenant_id"]
        res, report, started = p["result"], p["report"], p["started"]
        logs.extend(p["logs"])
        if p["error"] is not None:
            e = p["error"]
            lu.status(land_id, "failed", f"{type(e).__name__}: {str(e)[:380]}")
            logs.append(build_log_payload(processing_step="PROCESS_ERROR", step_status="failed",
                                          tenant_id=tenant_id, land_id=land_id, started_at=started,
                                          error_message=str(e)[:1000]))
            res.update(status="failed", error=str(e))
            results.append(res)
            continue
        if not p["rows"] and report.get("prior_accepted_in_window"):
            # Nothing NEW accepted, but an accepted optical measurement from an
            # earlier night is in the window: status and snapshot stay as they
            # are (before the ledger that scene was simply re-accepted).
            res["scene_evaluations"] = _evaluations(land_id, tenant_id, report, [])
            res.update(status="unchanged")
            results.append(res)
            continue
        if not p["rows"]:
            res["scene_evaluations"] = _evaluations(land_id, tenant_id, report, [])
            lu.status(land_id, "no_data", "no usable optical or radar acquisition in window")
            logs.append(build_log_payload(
                processing_step="PROCESS_SKIPPED", step_status="skipped", tenant_id=tenant_id,
                land_id=land_id, started_at=started,
                metadata={"reason": "no_usable_acquisition", "lookback_days": lookback,
                          "items_searched": report.get("items"),
                          "optical_rejects": report.get("optical_rejects", [])[:6],
                          "parcel_context": p["context_report"],
                          "water_layers": res["water_layers"],
                          "geometry_confidence": report.get("geometry_confidence")}))
            results.append(res)
            continue
        to_store.append(p)

    # 0. images - uploaded for the whole block at once, concurrently, AFTER
    #    measurement and BEFORE the rows are written, so each row records the
    #    true image state. A failed image clears only its image fields and keeps
    #    that scene out of the ledger (retried next night); numbers are never lost.
    jobs = []
    for p in pendings:
        if p["error"] is not None:
            continue
        for r in p["rows"]:
            for u in r.get("_uploads", []):
                jobs.append((u, ("row", p["land"]["id"], r.get("scene_id"))))
        for u in p.get("uploads", []):
            jobs.append((u, ("water", p["land"]["id"], None)))
        tu = p["land"].get("_thumbnail_upload")
        if tu:
            jobs.append((tu, ("thumb", p["land"]["id"], None)))
    failed_paths = set()
    if jobs:
        from concurrent.futures import ThreadPoolExecutor as _TPE
        from raster_io import upload_png as _upload_png
        from db import get_supabase_client as _client
        with _TPE(max_workers=IMAGE_UPLOAD_WORKERS) as up:
            futs = {up.submit(_upload_png, _client(), u["path"], u["data"]): u for u, _ in jobs}
            for f, u in futs.items():
                try:
                    ok = f.result()
                except Exception:
                    ok = None
                if not ok:
                    failed_paths.add(u["path"])
        COUNTERS.bump("images_uploaded", len(jobs) - len(failed_paths))
        COUNTERS.bump("images_failed", len(failed_paths))
    retry_scenes = set()                              # (land_id, scene_id) kept out of the ledger
    for p in pendings:
        lid = p["land"]["id"]
        for r in p["rows"]:
            ups = r.pop("_uploads", [])
            if any(u["path"] in failed_paths for u in ups):
                retry_scenes.add((lid, r.get("scene_id")))
                img = (r.get("metadata") or {}).get("image") or {}
                if r.get("image_url") in failed_paths:
                    r["image_url"] = None
                    img.pop("storage_path", None); img["upload_failed"] = True
                z = img.get("zones") or {}
                if z.get("storage_path") in failed_paths:
                    z.pop("storage_path", None); z["upload_failed"] = True
        for rec in p.get("water_records", []):
            if rec.get("image_path") in failed_paths:
                rec["image_path"] = None
                rec["image_metadata"] = {**(rec.get("image_metadata") or {}), "upload_failed": True}
                retry_scenes.add((lid, rec.get("scene_id")))
        tu = p["land"].pop("_thumbnail_upload", None)
        if tu and tu["path"] not in failed_paths:
            p["land"]["_thumbnail_path"] = tu["path"]
        elif tu:
            p["land"]["_thumbnail_pending"] = True      # try again on the next clean pass

    # water layers - one batch for the block (was one request per layer per scene)
    all_water = [rec for p in pendings if p["error"] is None for rec in p.get("water_records", [])]
    if all_water:
        retry_scenes |= upsert_water_layers_batch(all_water)

    # 1. observations - one batch for the block
    all_rows = [r for p in to_store for r in p["rows"]]
    written, new, failed = (upsert_observations_batch(all_rows, run_started_at=run_started)
                            if all_rows else ({}, {}, set()))
    stored = [p for p in to_store if p["land"]["id"] not in failed]
    for p in to_store:
        if p["land"]["id"] in failed:
            land_id, tenant_id = p["land"]["id"], p["land"]["tenant_id"]
            lu.status(land_id, "failed", "observation upsert failed")
            logs.append(build_log_payload(processing_step="PROCESS_ERROR", step_status="failed",
                                          tenant_id=tenant_id, land_id=land_id, started_at=p["started"],
                                          error_message="observation upsert failed"))
            p["result"].update(status="failed", error="observation upsert failed")
            results.append(p["result"])

    # 2. observed intelligence - only for observations that are stored
    intel_rows = [r for p in stored for r in p["rows"]
                  if r.get("observation_source") == "sentinel-2" and r.get("ndvi_value") is not None]
    iw, ifail = persist_observed_intelligence_batch(intel_rows) if intel_rows else ({}, {})

    # 3-5. per land: snapshot/status, ledger outcomes, logs
    for p in stored:
        land, rows, report, res, started = p["land"], p["rows"], p["report"], p["result"], p["started"]
        land_id, tenant_id = land["id"], land["tenant_id"]
        history = p["history"]
        try:
            res["scene_evaluations"] = _evaluations(land_id, tenant_id, report, rows)
            intelligence_written = int(iw.get(land_id, 0))
            intelligence_failed = int(ifail.get(land_id, 0))
            res["intelligence_written"], res["intelligence_failed"] = intelligence_written, intelligence_failed
            if intelligence_failed:
                logs.append(build_log_payload(
                    processing_step="INTELLIGENCE_PERSIST_ERROR", step_status="failed",
                    tenant_id=tenant_id, land_id=land_id, started_at=started,
                    error_message=f"{intelligence_failed} observed intelligence record(s) failed"))
            newest = max(rows, key=lambda r: (r["acquisition_date"], r.get("acquisition_time") or ""))
            optical = [r for r in rows if r.get("ndvi_value") is not None]
            trend = None
            if optical:
                newest_opt = max(optical, key=lambda r: (r["acquisition_date"], r.get("acquisition_time") or ""))
                newest_known = max((h["acquisition_date"] for h in history or [] if h.get("acquisition_date")),
                                   default=None)
                # Only ever move the land's snapshot FORWARD: with incremental
                # processing a late-arriving OLDER scene can be the only new row.
                if newest_known is None or newest_opt["acquisition_date"] >= newest_known:
                    lu.snapshot(
                        land_id=land_id, ndvi_value=newest_opt["ndvi_value"],
                        acquisition_date=newest_opt["acquisition_date"],
                        quality_score=newest_opt.get("quality_score"), source="sentinel-2",
                        # one-time true-colour land thumbnail (processor sets it only
                        # while the land has none) - replaces the paid Static Map
                        thumbnail_url=land.get("_thumbnail_path"),
                    )
                trend = classify_trend([
                    (date.fromisoformat(r["acquisition_date"]), r["ndvi_value"]) for r in optical
                ] + [
                    (date.fromisoformat(h["acquisition_date"]), h["ndvi_value"])
                    for h in history if h.get("ndvi_value") is not None
                    and h.get("scene_id") not in {r["scene_id"] for r in optical}
                ])
            else:
                lu.status(land_id, "completed",
                                 f"radar-only ({newest['acquisition_date']}); optical cache retained")
            w, n = written.get(land_id, 0), new.get(land_id, 0)
            if intelligence_failed > 0:
                res.update(rows=w, new_rows=n, status="failed", source=newest.get("observation_source"),
                           error=f"{intelligence_failed} accepted optical observation(s) failed intelligence persistence")
                logs.append(build_log_payload(
                    processing_step="PROCESS_DEGRADED", step_status="failed", tenant_id=tenant_id,
                    land_id=land_id, started_at=started, error_message=res["error"],
                    metadata={"observations_upserted": w, "intelligence_written": intelligence_written,
                              "intelligence_failed": intelligence_failed}))
            else:
                res.update(rows=w, new_rows=n, status="completed", source=newest.get("observation_source"))
                logs.append(build_log_payload(
                    processing_step="PROCESS_END", step_status="completed", tenant_id=tenant_id,
                    land_id=land_id, started_at=started,
                    metadata={"observations_upserted": w, "observations_new": n,
                              "intelligence_written": intelligence_written,
                              "intelligence_failed": intelligence_failed,
                              "source": newest.get("observation_source"),
                              "newest_acquisition": newest["acquisition_date"],
                              "optical_count": len(optical), "items_searched": report.get("items"),
                              "tile_duplicates_removed": report.get("deduped"),
                              "parcel_context": p["context_report"],
                              "water_layers": res["water_layers"],
                              "temporal_outliers": [r["acquisition_date"] for r in optical
                                                    if r.get("metadata", {}).get("temporal_outlier")],
                              "trend_21d": trend,
                              "geometry_confidence": report.get("geometry_confidence")}))
        except Exception as e:
            logger.exception(f"Land {land_id} failed during persistence")
            lu.status(land_id, "failed", f"{type(e).__name__}: {str(e)[:380]}")
            logs.append(build_log_payload(processing_step="PROCESS_ERROR", step_status="failed",
                                          tenant_id=tenant_id, land_id=land_id, started_at=started,
                                          error_message=str(e)[:1000]))
            res.update(status="failed", error=str(e))
            res["scene_evaluations"] = []        # never record "accepted" for an unfinished land
        results.append(res)

    if retry_scenes:
        for res in results:
            res["scene_evaluations"] = [e for e in res.get("scene_evaluations", [])
                                        if (e["land_id"], e["scene_id"]) not in retry_scenes]
        COUNTERS.bump("scenes_left_for_retry", len(retry_scenes))
    # thumbnails uploaded this block are saved even when no snapshot moved
    for p in pendings:
        if p["land"].get("_thumbnail_path"):
            lu.thumbnail(p["land"]["id"], p["land"]["_thumbnail_path"])
    lu.flush()
    log_steps_batch(logs)
    return results


def handle_land(land: dict, lookback: int, run_started: datetime, scenes=None, blocks=None,
                ledger=None) -> dict:
    """One land, measure then persist - kept for callers outside the block path."""
    return persist_block([measure_land(land, lookback, scenes, blocks, ledger)], lookback, run_started)[0]


def handle_block(block_lands: List[dict], block_geom, scenes, lookback: int,
                 run_started: datetime, incremental: bool = True) -> List[dict]:
    """Read each scene ONCE for this block of neighbouring lands, then process them.

    This is the change that makes the pipeline scale: reads become per
    tile-scene-block instead of per land. The measured 2026-09-22 run issued
    ~100 windowed reads per land to produce 1.5 observations; one block read
    serves every land inside it. Values are unchanged - tile_reader.subset_band
    reproduces read_band exactly, and any band the block could not supply falls
    back to the original per-parcel read.
    """
    # --- 1. ledger for the whole block in one or two requests --------------
    ledger_by_land = None
    if incremental and INCREMENTAL_SCENES and scenes is not None:
        from datetime import timedelta
        since = (date.today() - timedelta(days=lookback + 5)).isoformat()
        ledger_by_land = fetch_scene_ledger([l["id"] for l in block_lands], PROCESSING_IDENTITY, since)

    # --- 2. decide which lands have anything new to measure -----------------
    # A land is skipped ENTIRELY only when every scene covering it is already
    # measured for its current boundary AND one of them was accepted: that is
    # exactly the case where the pre-ledger pipeline re-measured identical
    # values and did not reach the Sentinel-1 fallback. No reads, no history
    # query, no log rows, no upserts. Lands without a prior acceptance still
    # run so the radar fallback behaves as before.
    to_process, results = [], []
    scene_ids = [getattr(s, "id", None) for s in (scenes or [])]
    for land in block_lands:
        led = (ledger_by_land or {}).get(land["id"]) if ledger_by_land is not None else None
        known = {}
        if led is not None and scene_ids:
            try:
                fp = geometry_fingerprint(resolve_geometry(land)[0])
            except Exception:
                fp = None
            # only outcomes recorded for THIS boundary count as "known"
            known = {sid: [o for f, o in led.get(sid, []) if f == fp] for sid in scene_ids} if fp else {}
            if fp and all(known.get(sid) for sid in scene_ids) \
                    and any("accepted" in v for v in known.values()):
                COUNTERS.bump("lands_unchanged")
                results.append({"land_id": land["id"], "rows": 0, "new_rows": 0, "source": None,
                                "status": "unchanged", "error": None, "context_enriched": 0,
                                "context_failed": 0, "water_layers": 0, "intelligence_written": 0,
                                "intelligence_failed": 0, "scene_evaluations": [],
                                "scenes_skipped_known": len(scene_ids)})
                continue
        to_process.append((land, led, known))

    # --- 3. read each still-needed scene ONCE for the block ------------------
    needed = set()
    for land, led, known in to_process:
        # fingerprint-aware: a redrawn boundary makes every scene "needed" again
        needed.update(sid for sid in scene_ids if not known.get(sid))
    blocks_by_scene = {}
    if TILE_BATCH_READS and scenes and block_geom is not None and to_process:
        bands = ["B04"] + [b for b in S2_BANDS_10M if b != "B04"] + list(S2_BANDS_20M) + ["SCL"]
        for scene in scenes:
            sid = getattr(scene, "id", None)
            if not sid or sid not in needed:
                continue                     # every land here already measured it
            try:
                blocks_by_scene[sid] = read_scene_block(scene, bands, block_geom,
                                                        extra_pad_m=CONTEXT_BLOCK_PAD_M)
            except Exception as exc:
                logger.warning(f"block read failed scene={sid}: {type(exc).__name__}: {exc}")

    # --- 4. measure every land from the shared blocks (no DB writes) --------
    histories = optical_history_batch([l["id"] for l, _, _ in to_process],
                                      TEMPORAL_LOOKBACK_DAYS) if to_process else {}
    # ONE radar search for the whole block, made only if some land actually
    # falls back to Sentinel-1 (lazily, on first use). Before this every such
    # land searched Planetary Computer on its own, every night.
    s1_cache = {}

    def block_s1_search():
        if "pairs" not in s1_cache:
            from sentinel_search import search_s1
            try:
                s1_cache["pairs"] = search_s1(block_geom) if block_geom is not None else None
            except Exception as exc:
                logger.warning(f"block S1 search failed: {type(exc).__name__}: {exc}")
                s1_cache["pairs"] = None
            COUNTERS.bump("s1_block_searches")
        return s1_cache["pairs"]

    # Radar VV/VH read ONCE per block for the newest covering pass (lazily, on
    # first use); every land in the block is subset from it exactly.
    s1_blocks = {}

    def s1_block_for(item):
        sid = getattr(item, "id", None)
        if block_geom is None or not sid:
            return None
        if sid not in s1_blocks:
            try:
                keys = {k.lower(): k for k in item.assets}
                s1_blocks[sid] = read_scene_block(item, [keys["vv"], keys["vh"]], block_geom,
                                                  categorical=(), extra_pad_m=0.0)
            except Exception as exc:
                logger.warning(f"S1 block read failed scene={sid}: {type(exc).__name__}: {exc}")
                s1_blocks[sid] = None
        return s1_blocks[sid]

    pendings = []
    try:
        for land, led, _known in to_process:
            pendings.append(measure_land(land, lookback, scenes, blocks_by_scene, ledger=led,
                                         history=histories.get(land["id"], []),
                                         s1_search=block_s1_search if block_geom is not None else None,
                                         s1_block_for=s1_block_for if block_geom is not None else None))
    finally:
        for blk in blocks_by_scene.values():
            release_block(blk)           # return bytes to the process-wide budget
        blocks_by_scene.clear()
        for blk in s1_blocks.values():
            if blk is not None:
                release_block(blk)
        s1_blocks.clear()

    # --- 5. persist the whole block in batched, ordered writes --------------
    evaluations = []
    for r in persist_block(pendings, lookback, run_started):
        evaluations.extend(r.pop("scene_evaluations", []) or [])
        results.append(r)
    if evaluations:
        COUNTERS.bump("ledger_rows_written", record_scene_evaluations(evaluations))
    return results


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tenant", default=None, help="restrict to one tenant")
    ap.add_argument("--lookback", type=int, default=LOOKBACK_DAYS)
    ap.add_argument("--backfill", action="store_true", help=f"use BACKFILL_DAYS={BACKFILL_DAYS} instead")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--reprocess", action="store_true",
                    help="ignore the scene ledger and re-measure every scene in the window")
    args = ap.parse_args()
    lookback = BACKFILL_DAYS if args.backfill else args.lookback
    run_started = datetime.now(timezone.utc)

    total = count_eligible_lands(args.tenant)
    logger.info(f"NDVI {PIPELINE_VERSION} start | eligible_lands={total} | lookback={lookback}d "
                f"| tenant={args.tenant or 'ALL'} | parcel_context=enabled | water_layers=enabled | intelligence_persist=enabled")
    if args.dry_run:
        for i, land in enumerate(iter_lands(args.tenant)):
            logger.info(f"[dry-run] {i+1}/{total} {land['id']} crop={land.get('current_crop')}")
        return 0

    stats = {"lands": 0, "completed": 0, "skipped": 0, "failed": 0,
             "observations": 0, "new_observations": 0, "unverified_new": 0,
             "optical": 0, "radar": 0, "context_enriched": 0, "context_failed": 0,
             "water_layers": 0, "intelligence_written": 0, "intelligence_failed": 0}
    # PASS 1 - group using light key columns only (no geometry in memory).
    groups = group_lands_by_tile(iter_land_keys(args.tenant))
    log_group_plan(groups)
    incremental = not args.reprocess

    def work_items():
        """PASS 2 - one group at a time: fetch its geometry, search its scenes
        once, plan its read-blocks, yield block work. Only the current group's
        full rows are held here; blocks in flight hold their own lands."""
        for tile, light in groups.items():
            tile_lands = fetch_lands_by_ids([l["id"] for l in light])
            if not tile_lands:
                continue
            if tile == "__untiled__":
                for land in tile_lands:
                    yield (handle_block, [land], None, None, lookback, run_started, incremental)
                continue
            try:
                scenes = scenes_for_group(tile_lands, lookback_days=lookback)
                logger.info(f"Group {tile}: {len(scenes) if scenes is not None else 'per-land'} "
                            f"scene(s) for {len(tile_lands)} land(s)")
            except Exception:
                logger.exception(f"Group {tile} search failed; falling back per-land")
                scenes = None
            geoms = {}
            for land in tile_lands:
                try:
                    geoms[land["id"]] = resolve_geometry(land)[0]
                except Exception as exc:
                    logger.warning(f"geometry unavailable for land {land['id']}: {exc}")
            blocks = plan_blocks([l for l in tile_lands if l["id"] in geoms], geoms,
                                 max_span_m=BLOCK_SPAN_M,
                                 max_lands_per_block=BLOCK_MAX_LANDS) if geoms else []
            logger.info(f"Group {tile}: {len(blocks)} read-block(s) for {len(tile_lands)} land(s)")
            for block_lands, block_geom in blocks:
                block_scenes = scenes_for_block(scenes, block_geom) if scenes is not None else None
                yield (handle_block, block_lands, block_geom, block_scenes, lookback, run_started, incremental)
            for land in tile_lands:
                if land["id"] not in geoms:
                    yield (handle_block, [land], None, scenes, lookback, run_started, incremental)

    def aggregate(results):
        for r in results:
            stats["lands"] += 1
            if r["status"] == "unchanged":
                stats["unchanged"] = stats.get("unchanged", 0) + 1
            else:
                stats[r["status"] if r["status"] in ("completed", "failed") else "skipped"] += 1
            stats["scenes_skipped_known"] = stats.get("scenes_skipped_known", 0) + int(r.get("scenes_skipped_known") or 0)
            stats["observations"] += r["rows"]
            stats["context_enriched"] += r.get("context_enriched", 0)
            stats["context_failed"] += r.get("context_failed", 0)
            stats["context_not_applicable"] = stats.get("context_not_applicable", 0) + r.get("context_not_applicable", 0)
            stats["water_layers"] += r.get("water_layers", 0)
            stats["intelligence_written"] += r.get("intelligence_written", 0)
            stats["intelligence_failed"] += r.get("intelligence_failed", 0)
            if r["new_rows"] >= 0:
                stats["new_observations"] += r["new_rows"]
            else:
                stats["unverified_new"] += 1
            if r["source"] == "sentinel-2":
                stats["optical"] += 1
            elif r["source"] == "sentinel-1":
                stats["radar"] += 1

    # Bounded in-flight work: at most 2 x TILE_WORKERS blocks are queued at a
    # time, so memory is set by the worker count, not by how many lands exist.
    max_inflight = max(2 * TILE_WORKERS, 2)
    with ThreadPoolExecutor(max_workers=TILE_WORKERS) as pool:
        inflight = set()
        for fn, *fargs in work_items():
            inflight.add(pool.submit(fn, *fargs))
            if len(inflight) >= max_inflight:
                done, inflight = wait(inflight, return_when=FIRST_COMPLETED)
                for fut in done:
                    aggregate(fut.result())
        for fut in as_completed(inflight):
            aggregate(fut.result())

    duration = (datetime.now(timezone.utc) - run_started).total_seconds()
    # Skip rate over lands actually ATTEMPTED. Lands the ledger showed as
    # unchanged were not attempted; counting them would dilute the rate and
    # hide a regional outage behind a quiet night elsewhere.
    attempted = stats["lands"] - stats.get("unchanged", 0)
    skip_rate = round(100.0 * stats["skipped"] / attempted, 1) if attempted else 0.0
    summary = {
        "run_started_at": run_started.isoformat(), "duration_seconds": round(duration, 1),
        "lookback_days": lookback, "lands_eligible": total,
        "lands_processed": stats["lands"], "lands_completed": stats["completed"],
        "lands_skipped": stats["skipped"], "lands_failed": stats["failed"],
        "skip_rate_pct": skip_rate, "observations_written": stats["observations"],
        "lands_via_optical": stats["optical"], "lands_via_radar": stats["radar"],
        "tenant_id": args.tenant,
        "notes": {"observations_new": stats["new_observations"],
                  "lands_new_count_unverified": stats["unverified_new"],
                  "pipeline_version": PIPELINE_VERSION,
                  "processing_identity": PROCESSING_IDENTITY,
                  "parcel_context": "enabled; observed_context_only",
                  "water_layers": "enabled; observed spectral evidence only",
                  "water_layer_rows_written": stats["water_layers"],
                  # Canary telemetry (release audit 2026-09-22): compare a
                  # TILE_WORKERS=10 run against the 4-worker baseline on these,
                  # not on elapsed time alone. A faster run that raises
                  # read_errors_429 or read_errors_5xx_or_timeout is not a win.
                  "io": COUNTERS.snapshot(),
                  "tile_workers": TILE_WORKERS,
                  "lands_unchanged": stats.get("unchanged", 0),
                  "scenes_skipped_known": stats.get("scenes_skipped_known", 0),
                  "incremental": bool(INCREMENTAL_SCENES and not args.reprocess),
                  "tile_batch_reads": TILE_BATCH_READS,
                  "parcel_context_rows_enriched": stats["context_enriched"],
                  "parcel_context_rows_failed": stats["context_failed"],
                  "parcel_context_rows_not_applicable": stats.get("context_not_applicable", 0),
                  "intelligence_rows_written": stats["intelligence_written"],
                  "intelligence_rows_failed": stats["intelligence_failed"]},
    }
    write_run_summary(summary)
    logger.info("=" * 72)
    logger.info(f"NDVI {PIPELINE_VERSION} finished in {duration:.0f}s")
    logger.info(f"  lands: {stats['lands']} processed ({stats['completed']} ok / {stats.get('unchanged', 0)} unchanged / {stats['skipped']} skipped / {stats['failed']} failed) skip_rate={skip_rate}% of attempted")
    logger.info(f"  observations: {stats['observations']} upserted, {stats['new_observations']} NEW (optical lands {stats['optical']} / radar {stats['radar']})")
    logger.info(f"  intelligence: {stats['intelligence_written']} observed records upserted / {stats['intelligence_failed']} failed")
    logger.info(f"  parcel context: {stats['context_enriched']} rows enriched / {stats['context_failed']} failed / "
                f"{stats.get('context_not_applicable', 0)} not applicable (field larger than context area)")
    logger.info(f"  water layers: {stats['water_layers']} observed layer rows")
    logger.info("=" * 72)
    if stats["observations"] == 0 and stats.get("unchanged", 0) == 0:
        # A land can only be 'unchanged' if the search RETURNED scenes that the
        # ledger already knows - so a STAC outage (no scenes) still lands here.
        logger.error("ZERO acquisitions accepted. Treating as FAILURE. Check STAC availability, cloud conditions, SCL thresholds, S1 fallback.")
        return 2
    if stats["observations"] == 0:
        logger.info(f"No new acquisitions tonight: {stats.get('unchanged', 0)} land(s) unchanged "
                    f"(every scene in the window already measured). Healthy run.")
    if stats["intelligence_failed"] > 0:
        logger.error(f"{stats['intelligence_failed']} accepted optical observation(s) failed intelligence persistence.")
        return 3
    if stats["failed"] > 0 and stats["failed"] >= stats["completed"]:
        logger.error(f"{stats['failed']} lands failed vs {stats['completed']} completed - degraded run.")
    if skip_rate >= 80.0:
        logger.error(f"Skip rate {skip_rate}% >= 80% - degraded run.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        logger.exception("FATAL")
        sys.exit(1)
