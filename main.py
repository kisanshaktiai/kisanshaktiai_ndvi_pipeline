"""NDVI pipeline entrypoint with separate observed parcel-context evidence."""

import sys
import argparse
from datetime import datetime, timezone, date
from concurrent.futures import ThreadPoolExecutor, as_completed

from db import (
    iter_lands, count_eligible_lands, upsert_observations, optical_history,
    update_land_snapshot, mark_land_status, log_step, write_run_summary,
)
from processor import PIPELINE_VERSION
from context_enrichment import process_land_with_context
from tile_grouping import group_lands_by_tile, scenes_for_group, log_group_plan
from phenology import classify_trend
from config import TILE_WORKERS, LOOKBACK_DAYS, BACKFILL_DAYS, TEMPORAL_LOOKBACK_DAYS
from logger import logger


def handle_land(land: dict, lookback: int, run_started: datetime, scenes=None) -> dict:
    land_id, tenant_id = land["id"], land["tenant_id"]
    started = datetime.now(timezone.utc)
    result = {"land_id": land_id, "rows": 0, "new_rows": 0, "source": None,
              "status": "skipped", "error": None, "context_enriched": 0,
              "context_failed": 0}

    log_step(processing_step="PROCESS_START", step_status="started",
             tenant_id=tenant_id, land_id=land_id, started_at=started)
    try:
        history = optical_history(land_id, TEMPORAL_LOOKBACK_DAYS)
        rows, report = process_land_with_context(
            land, lookback_days=lookback, scenes=scenes, history=history
        )
        context_report = report.get("parcel_context") or {}
        result["context_enriched"] = int(context_report.get("rows_enriched") or 0)
        result["context_failed"] = int(context_report.get("rows_failed") or 0)

        for err in report.get("scene_errors", []):
            log_step(processing_step="SCENE_ERROR", step_status="failed",
                     tenant_id=tenant_id, land_id=land_id, started_at=started,
                     error_message=str(err.get("error"))[:1000],
                     metadata={k: v for k, v in err.items() if k != "error"})

        if not rows:
            mark_land_status(land_id, "no_data",
                             "no usable optical or radar acquisition in window")
            log_step(processing_step="PROCESS_SKIPPED", step_status="skipped",
                     tenant_id=tenant_id, land_id=land_id, started_at=started,
                     metadata={"reason": "no_usable_acquisition",
                               "lookback_days": lookback,
                               "items_searched": report.get("items"),
                               "optical_rejects": report.get("optical_rejects", [])[:6],
                               "parcel_context": context_report,
                               "geometry_confidence": report.get("geometry_confidence")})
            return result

        written, new = upsert_observations(rows, run_started_at=run_started)
        newest = max(rows, key=lambda r: (r["acquisition_date"], r.get("acquisition_time") or ""))
        optical = [r for r in rows if r.get("ndvi_value") is not None]
        trend = None

        if optical:
            newest_opt = max(optical, key=lambda r: (r["acquisition_date"], r.get("acquisition_time") or ""))
            update_land_snapshot(
                land_id=land_id, ndvi_value=newest_opt["ndvi_value"],
                acquisition_date=newest_opt["acquisition_date"],
                quality_score=newest_opt.get("quality_score"), source="sentinel-2",
            )
            trend = classify_trend([
                (date.fromisoformat(r["acquisition_date"]), r["ndvi_value"])
                for r in optical
            ] + [
                (date.fromisoformat(h["acquisition_date"]), h["ndvi_value"])
                for h in history if h.get("ndvi_value") is not None
                and h.get("scene_id") not in {r["scene_id"] for r in optical}
            ])
        else:
            mark_land_status(land_id, "completed",
                             f"radar-only ({newest['acquisition_date']}); optical cache retained")

        result.update(rows=written, new_rows=new, status="completed",
                      source=newest.get("observation_source"))
        log_step(processing_step="PROCESS_END", step_status="completed",
                 tenant_id=tenant_id, land_id=land_id, started_at=started,
                 metadata={"observations_upserted": written, "observations_new": new,
                           "source": newest.get("observation_source"),
                           "newest_acquisition": newest["acquisition_date"],
                           "optical_count": len(optical), "items_searched": report.get("items"),
                           "tile_duplicates_removed": report.get("deduped"),
                           "parcel_context": context_report,
                           "temporal_outliers": [r["acquisition_date"] for r in optical
                                                 if r.get("metadata", {}).get("temporal_outlier")],
                           "trend_21d": trend,
                           "geometry_confidence": report.get("geometry_confidence")})
        return result
    except Exception as e:
        logger.exception(f"Land {land_id} failed")
        mark_land_status(land_id, "failed", f"{type(e).__name__}: {str(e)[:380]}")
        log_step(processing_step="PROCESS_ERROR", step_status="failed",
                 tenant_id=tenant_id, land_id=land_id, started_at=started,
                 error_message=str(e)[:1000])
        result.update(status="failed", error=str(e))
        return result


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tenant", default=None, help="restrict to one tenant")
    ap.add_argument("--lookback", type=int, default=LOOKBACK_DAYS)
    ap.add_argument("--backfill", action="store_true", help=f"use BACKFILL_DAYS={BACKFILL_DAYS} instead")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    lookback = BACKFILL_DAYS if args.backfill else args.lookback
    run_started = datetime.now(timezone.utc)

    total = count_eligible_lands(args.tenant)
    logger.info(f"NDVI {PIPELINE_VERSION} start | eligible_lands={total} | lookback={lookback}d "
                f"| tenant={args.tenant or 'ALL'} | parcel_context=enabled")
    if args.dry_run:
        for i, land in enumerate(iter_lands(args.tenant)):
            logger.info(f"[dry-run] {i+1}/{total} {land['id']} crop={land.get('current_crop')}")
        return 0

    stats = {"lands": 0, "completed": 0, "skipped": 0, "failed": 0,
             "observations": 0, "new_observations": 0, "unverified_new": 0,
             "optical": 0, "radar": 0, "context_enriched": 0, "context_failed": 0}
    groups = group_lands_by_tile(iter_lands(args.tenant))
    log_group_plan(groups)

    with ThreadPoolExecutor(max_workers=TILE_WORKERS) as pool:
        futures = {}
        for tile, tile_lands in groups.items():
            if tile == "__untiled__":
                for land in tile_lands:
                    futures[pool.submit(handle_land, land, lookback, run_started, None)] = land["id"]
                continue
            try:
                scenes = scenes_for_group(tile_lands, lookback_days=lookback)
                logger.info(f"Tile {tile}: {len(scenes) if scenes is not None else 'per-land'} scene(s) for {len(tile_lands)} land(s)")
            except Exception:
                logger.exception(f"Tile {tile} search failed; falling back per-land")
                scenes = None
            for land in tile_lands:
                futures[pool.submit(handle_land, land, lookback, run_started, scenes)] = land["id"]

        for fut in as_completed(futures):
            r = fut.result()
            stats["lands"] += 1
            stats[r["status"] if r["status"] in ("completed", "failed") else "skipped"] += 1
            stats["observations"] += r["rows"]
            stats["context_enriched"] += r.get("context_enriched", 0)
            stats["context_failed"] += r.get("context_failed", 0)
            if r["new_rows"] >= 0:
                stats["new_observations"] += r["new_rows"]
            else:
                stats["unverified_new"] += 1
            if r["source"] == "sentinel-2":
                stats["optical"] += 1
            elif r["source"] == "sentinel-1":
                stats["radar"] += 1

    duration = (datetime.now(timezone.utc) - run_started).total_seconds()
    skip_rate = round(100.0 * stats["skipped"] / stats["lands"], 1) if stats["lands"] else 0.0
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
                  "parcel_context": "enabled; observed_context_only",
                  "parcel_context_rows_enriched": stats["context_enriched"],
                  "parcel_context_rows_failed": stats["context_failed"]},
    }
    write_run_summary(summary)
    logger.info("=" * 72)
    logger.info(f"NDVI {PIPELINE_VERSION} finished in {duration:.0f}s")
    logger.info(f"  lands: {stats['lands']} processed ({stats['completed']} ok / {stats['skipped']} skipped / {stats['failed']} failed) skip_rate={skip_rate}%")
    logger.info(f"  observations: {stats['observations']} upserted, {stats['new_observations']} NEW (optical lands {stats['optical']} / radar {stats['radar']})")
    logger.info(f"  parcel context: {stats['context_enriched']} rows enriched / {stats['context_failed']} failed; observed_context_only")
    logger.info("=" * 72)
    if stats["observations"] == 0:
        logger.error("ZERO acquisitions accepted. Treating as FAILURE. Check STAC availability, cloud conditions, SCL thresholds, S1 fallback.")
        return 2
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
