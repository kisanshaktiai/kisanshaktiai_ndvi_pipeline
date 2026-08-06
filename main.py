"""
main.py - NDVI pipeline v2 entrypoint.

CHANGES FROM v1 THAT MATTER MOST
--------------------------------
1. Rows are stamped with the TRUE acquisition date, never date.today()  (P-01)
2. ONE ROW PER ACQUISITION, not one composite per run                   (P-02)
3. Every land is processed via pagination, not the first 100            (P-08)
4. Sentinel-1 fallback keeps the platform alive through monsoon         (P-14)
5. THE RUN FAILS LOUDLY when it produces no observations                (obs.)

Exit codes:
  0  observations written
  1  fatal error
  2  ran cleanly but wrote ZERO observations  <- this is a FAILURE
"""

import sys
import argparse
from datetime import datetime, timezone, date
from concurrent.futures import ThreadPoolExecutor, as_completed

from db import (
    iter_lands, count_eligible_lands, upsert_observations,
    update_land_snapshot, mark_land_status, log_step, write_run_summary,
)
from processor import process_land
from tile_grouping import group_lands_by_tile, scenes_for_group, log_group_plan
from phenology import compute_anomaly, classify_trend
from config import TILE_WORKERS, LOOKBACK_DAYS, BACKFILL_DAYS
from logger import logger


def handle_land(land: dict, lookback: int, scenes=None) -> dict:
    land_id, tenant_id = land["id"], land["tenant_id"]
    started = datetime.now(timezone.utc)

    result = {"land_id": land_id, "rows": 0, "source": None,
              "status": "skipped", "error": None}

    log_step(processing_step="PROCESS_START", step_status="started",
             tenant_id=tenant_id, land_id=land_id, started_at=started)

    try:
        rows = process_land(land, lookback_days=lookback, scenes=scenes)

        if not rows:
            # An honest skip: the reason is recorded, not silently swallowed.
            mark_land_status(land_id, "no_data",
                             "no usable optical or radar acquisition in window")
            log_step(processing_step="PROCESS_SKIPPED", step_status="skipped",
                     tenant_id=tenant_id, land_id=land_id, started_at=started,
                     metadata={"reason": "no_usable_acquisition",
                               "lookback_days": lookback})
            return result

        written = upsert_observations(rows)

        newest = max(rows, key=lambda r: r["acquisition_date"])
        optical = [r for r in rows if r.get("ndvi_value") is not None]

        if optical:
            newest_opt = max(optical, key=lambda r: r["acquisition_date"])
            update_land_snapshot(
                land_id=land_id,
                ndvi_value=newest_opt["ndvi_value"],
                acquisition_date=newest_opt["acquisition_date"],
                quality_score=newest_opt.get("quality_score"),
                source=newest_opt.get("observation_source", "sentinel-2"),
            )

            # Stage-relative interpretation is computed but NOT written into
            # ndvi_data; it belongs to the decision layer and is recomputed
            # there from authoritative crop/DAS. Logged here for traceability.
            # Stage-relative interpretation via the canonical DB resolver.
            # Computed for traceability only - NOT written to ndvi_data.
            # It belongs to the decision layer and is recomputed there from
            # authoritative crop/stage context.
            anomaly = compute_anomaly(
                ndvi=newest_opt["ndvi_value"],
                crop_raw=land.get("current_crop"),
                cultivation_method=land.get("cultivation_method"),
                sow_date=land.get("sowing_date") or land.get("last_sowing_date"),
                transplant_date=land.get("transplant_date"),
                crop_cycle=land.get("crop_cycle"),
                land_id=land_id,
            )
            trend = classify_trend([
                (date.fromisoformat(r["acquisition_date"]), r["ndvi_value"])
                for r in sorted(optical, key=lambda r: r["acquisition_date"])
            ])
            result["anomaly_status"] = anomaly.get("status")
            result["anomaly_reason"] = anomaly.get("reason")
            result["stage_code"] = anomaly.get("stage_code")
            result["stage_confidence"] = anomaly.get("stage_confidence")
            result["trend"] = trend["direction"]
        else:
            update_land_snapshot(
                land_id=land_id, ndvi_value=None,
                acquisition_date=newest["acquisition_date"],
                quality_score=newest.get("quality_score"),
                source="sentinel-1",
            )

        result.update(rows=written, status="completed",
                      source=newest.get("observation_source"))

        log_step(processing_step="PROCESS_END", step_status="completed",
                 tenant_id=tenant_id, land_id=land_id, started_at=started,
                 metadata={"observations_written": written,
                           "source": newest.get("observation_source"),
                           "newest_acquisition": newest["acquisition_date"],
                           "optical_count": len(optical)})
        return result

    except Exception as e:
        logger.exception(f"Land {land_id} failed")
        mark_land_status(land_id, "failed", str(e)[:400])
        log_step(processing_step="PROCESS_ERROR", step_status="failed",
                 tenant_id=tenant_id, land_id=land_id, started_at=started,
                 error_message=str(e)[:1000])
        result.update(status="failed", error=str(e))
        return result


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tenant", default=None, help="restrict to one tenant")
    ap.add_argument("--lookback", type=int, default=LOOKBACK_DAYS)
    ap.add_argument("--backfill", action="store_true",
                    help=f"use BACKFILL_DAYS={BACKFILL_DAYS} instead")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    lookback = BACKFILL_DAYS if args.backfill else args.lookback
    run_started = datetime.now(timezone.utc)

    total = count_eligible_lands(args.tenant)
    logger.info(f"NDVI v2 start | eligible_lands={total} | lookback={lookback}d "
                f"| tenant={args.tenant or 'ALL'}")

    if args.dry_run:
        for i, land in enumerate(iter_lands(args.tenant)):
            logger.info(f"[dry-run] {i+1}/{total} {land['id']} "
                        f"crop={land.get('current_crop')}")
        return 0

    stats = {"lands": 0, "completed": 0, "skipped": 0, "failed": 0,
             "observations": 0, "optical": 0, "radar": 0}

    # TILE-GROUPED EXECUTION
    # One STAC search per MGRS tile instead of one per land. Scenes are
    # fetched once and clipped per field - the correct synthesis of both
    # repositories (see tile_grouping.py).
    groups = group_lands_by_tile(iter_lands(args.tenant))
    log_group_plan(groups)

    with ThreadPoolExecutor(max_workers=TILE_WORKERS) as pool:
        futures = {}
        for tile, tile_lands in groups.items():
            if tile == "__untiled__":
                for land in tile_lands:
                    futures[pool.submit(handle_land, land, lookback, None)] = land["id"]
                continue
            try:
                scenes = scenes_for_group(tile_lands, lookback_days=lookback)
                logger.info(f"Tile {tile}: {len(scenes)} scene(s) for {len(tile_lands)} land(s)")
            except Exception:
                logger.exception(f"Tile {tile} search failed; falling back per-land")
                scenes = None
            for land in tile_lands:
                futures[pool.submit(handle_land, land, lookback, scenes)] = land["id"]

        for fut in as_completed(futures):
            r = fut.result()
            stats["lands"] += 1
            stats[r["status"] if r["status"] in ("completed", "failed") else "skipped"] += 1
            stats["observations"] += r["rows"]
            if r["source"] == "sentinel-2":
                stats["optical"] += 1
            elif r["source"] == "sentinel-1":
                stats["radar"] += 1

    duration = (datetime.now(timezone.utc) - run_started).total_seconds()
    skip_rate = round(100.0 * stats["skipped"] / stats["lands"], 1) if stats["lands"] else 0.0

    summary = {
        "run_started_at": run_started.isoformat(),
        "duration_seconds": round(duration, 1),
        "lookback_days": lookback,
        "lands_eligible": total,
        "lands_processed": stats["lands"],
        "lands_completed": stats["completed"],
        "lands_skipped": stats["skipped"],
        "lands_failed": stats["failed"],
        "skip_rate_pct": skip_rate,
        "observations_written": stats["observations"],
        "lands_via_optical": stats["optical"],
        "lands_via_radar": stats["radar"],
    }
    write_run_summary(summary)

    logger.info("=" * 72)
    logger.info(f"NDVI v2 finished in {duration:.0f}s")
    logger.info(f"  lands: {stats['lands']} processed "
                f"({stats['completed']} ok / {stats['skipped']} skipped / "
                f"{stats['failed']} failed)  skip_rate={skip_rate}%")
    logger.info(f"  observations written: {stats['observations']} "
                f"(optical {stats['optical']} / radar {stats['radar']})")
    logger.info("=" * 72)

    # ---- THE OBSERVABILITY FIX ---------------------------------------
    # v1 exited 0 whether it wrote 2,481 rows or none. GitHub Actions
    # painted every monsoon night green while the database received
    # nothing for 29 days. A run that produces no observations is a
    # FAILED run and must surface as one.
    if stats["observations"] == 0:
        logger.error(
            "ZERO observations written. Treating as FAILURE. "
            "Check: STAC availability, cloud conditions, SCL thresholds, "
            "and whether Sentinel-1 fallback is enabled."
        )
        return 2

    if skip_rate >= 80.0:
        logger.error(f"Skip rate {skip_rate}% >= 80% - degraded run.")

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        logger.exception("FATAL")
        sys.exit(1)
