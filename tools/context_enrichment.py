"""Pipeline adapter that enriches accepted observations with same-scene context.

This keeps the existing processor measurement path untouched while ensuring
context is actually extracted and persisted in metadata for every accepted
optical acquisition. It reuses the exact STAC scene selected by the processor.
"""

from __future__ import annotations

from typing import List, Optional, Tuple

from parcel_context import extract_parcel_context, add_parcel_delta
from raster_utils import resolve_geometry
from sentinel_search import search_s2
from processor import process_land, dedupe_acquisitions
from logger import logger


def process_land_with_context(
    land: dict,
    lookback_days: int = None,
    scenes: Optional[List] = None,
    history: Optional[List[dict]] = None,
    context_area_m2: float = 4046.8564224,
    blocks: Optional[dict] = None,
    ledger: Optional[dict] = None,
) -> Tuple[List[dict], dict]:
    """Run the existing land processor, then attach same-scene context evidence."""
    selected_scenes = scenes
    if selected_scenes is None:
        geom, _ = resolve_geometry(land)
        selected_scenes = dedupe_acquisitions(
            search_s2(geom, days=lookback_days), geom
        )

    rows, report = process_land(
        land,
        lookback_days=lookback_days,
        scenes=selected_scenes,
        history=history,
        blocks=blocks,
        ledger=ledger,
    )

    if not rows:
        return rows, report

    try:
        parcel_geom, _ = resolve_geometry(land)
    except Exception as exc:
        logger.warning("Context skipped for land %s: geometry unavailable: %s", land["id"], exc)
        return rows, report

    by_scene = {getattr(scene, "id", None): scene for scene in (selected_scenes or [])}
    enriched = 0
    context_errors = 0
    # Reason counts travel in the report -> ndvi_processing_logs metadata, so a
    # drop in cohort coverage is diagnosable from the database rather than only
    # from the console (10 of 22 enrichments failed on 2026-09-22 with no
    # recorded reason).
    reasons: dict = {}

    def _note(reason: str) -> None:
        reasons[reason] = reasons.get(reason, 0) + 1

    for row in rows:
        if row.get("ndvi_value") is None:
            continue
        scene = by_scene.get(row.get("scene_id"))
        if scene is None:
            context_errors += 1
            _note("scene_not_in_selected_set")
            logger.warning(
                "Context skipped for land %s scene %s: scene not available in selected set",
                land["id"], row.get("scene_id"),
            )
            continue
        try:
            context = extract_parcel_context(
                scene,
                parcel_geom_wgs84=parcel_geom,
                target_area_m2=context_area_m2,
                block=(blocks or {}).get(row.get("scene_id")),
            )
            if context is None:
                context_errors += 1
                _note("extractor_returned_none")
                continue
            row.setdefault("metadata", {})["parcel_context"] = add_parcel_delta(
                context, row.get("ndvi_value")
            )
            enriched += 1
        except Exception as exc:
            context_errors += 1
            _note(type(exc).__name__)
            logger.warning(
                "Context extraction failed land=%s scene=%s: %s: %s",
                land["id"], row.get("scene_id"), type(exc).__name__, str(exc)[:240],
            )

    report["parcel_context"] = {
        "enabled": True,
        "target_total_area_m2": context_area_m2,
        "rows_enriched": enriched,
        "rows_failed": context_errors,
        "reasons": reasons,
        "measurement_unchanged": True,
        "status": "observed_context_only",
    }
    return rows, report
