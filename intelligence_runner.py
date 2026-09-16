"""Populate ndvi_intelligence from accepted observed NDVI measurements.

This is an explicit post-measurement stage. It does not train or infer an ML
model. It promotes only accepted observed evidence and optional same-scene
parcel context already stored in ndvi_data.metadata. Prediction remains
fail-closed until independent validation data exists.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone

from db import supabase, with_retry, log_step
from ndvi_intelligence_writer import build_observed_intelligence

PAGE_SIZE = 500
BATCH_SIZE = 100


def iter_observations(tenant_id=None, since=None):
    offset = 0
    while True:
        q = (supabase.table("ndvi_data")
             .select("*")
             .eq("observation_source", "sentinel-2")
             .eq("observation_type", "observed")
             .not_.is_("ndvi_value", "null")
             .gte("quality_score", 0.55)
             .order("acquisition_date", desc=False)
             .order("scene_id", desc=False)
             .range(offset, offset + PAGE_SIZE - 1))
        if tenant_id:
            q = q.eq("tenant_id", tenant_id)
        if since:
            q = q.gte("acquisition_date", since)
        rows = with_retry(lambda: q.execute().data or [],
                          what=f"read NDVI intelligence source page {offset}", attempts=3)
        if not rows:
            return
        for row in rows:
            yield row
        if len(rows) < PAGE_SIZE:
            return
        offset += PAGE_SIZE


def run(tenant_id=None, since=None) -> dict:
    started = datetime.now(timezone.utc)
    read_count = 0
    written = 0
    skipped = 0

    batch = []
    for row in iter_observations(tenant_id=tenant_id, since=since):
        read_count += 1
        record = build_observed_intelligence(row)
        if record is None:
            skipped += 1
            continue
        batch.append(record)
        if len(batch) >= BATCH_SIZE:
            with_retry(
                lambda b=batch: supabase.table("ndvi_intelligence").upsert(
                    b, on_conflict="tenant_id,land_id,scene_id"
                ).execute(),
                what=f"persist NDVI intelligence batch ({len(batch)})",
                attempts=3,
            )
            written += len(batch)
            batch = []

    if batch:
        with_retry(
            lambda b=batch: supabase.table("ndvi_intelligence").upsert(
                b, on_conflict="tenant_id,land_id,scene_id"
            ).execute(),
            what=f"persist NDVI intelligence batch ({len(batch)})",
            attempts=3,
        )
        written += len(batch)

    result = {
        "source_rows_read": read_count,
        "intelligence_rows_upserted": written,
        "source_rows_skipped": skipped,
        "tenant_id": tenant_id,
        "since": since,
        "prediction_enabled": False,
        "started_at": started.isoformat(),
    }
    log_step(
        processing_step="INTELLIGENCE_PERSIST",
        step_status="completed",
        tenant_id=tenant_id,
        started_at=started,
        metadata=result,
    )
    return result


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tenant", default=None)
    ap.add_argument("--since", default=None, help="minimum acquisition date YYYY-MM-DD")
    args = ap.parse_args()
    result = run(args.tenant, args.since)
    print(result)
    if result["source_rows_read"] and result["intelligence_rows_upserted"] == 0:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
