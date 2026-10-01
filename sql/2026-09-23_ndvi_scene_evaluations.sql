-- =====================================================================
-- ndvi_scene_evaluations: which (land, scene) pairs the pipeline has
-- already measured, so it stops re-measuring them every night.
-- Project: qfklkkzxemsbeniyugiz   Repo: kisanshaktiai_ndvi_pipeline
-- STATUS: REVIEW BEFORE APPLYING. The pipeline runs correctly WITHOUT this
-- table (it detects absence and simply re-evaluates everything, as today).
--
-- WHY (measured in this database, 2026-09-17 .. 2026-09-23):
--   genuinely new optical rows per night: 0, 0, 2, 0, 0, 0
--   while every run re-read and re-measured all ~7 scenes in the 20-day
--   window for all 30 lands. Sentinel-2 revisits every ~5 days, so each
--   scene is re-evaluated 4-8 times. Rejected (cloudy) scenes are not
--   stored in ndvi_data at all, so without a ledger they can never be
--   recognised as already-evaluated.
--
-- WHAT MAKES SKIPPING SAFE (no measurement is ever lost):
--   * key includes pipeline_version  -> any science change re-evaluates all
--   * key includes geometry_fingerprint -> a farmer editing the boundary
--     re-evaluates that land
--   * Sentinel-2 item ids embed the processing timestamp, so a reprocessed
--     product arrives as a NEW scene_id and is evaluated
--   * only DETERMINISTIC outcomes are recorded (accepted / rejected by the
--     quality gate). Transient read failures (429, 5xx, timeout) are NOT
--     recorded, so they retry next night.
--   * an accepted outcome is recorded only after its ndvi_data upsert
--     succeeded.
--   * `python main.py --reprocess` ignores the ledger entirely.
--
-- SIZE: one narrow row per (land, scene). At 1M lands x ~70 S2 scenes/year
-- that is ~70M rows/year; the pipeline only ever reads rows whose
-- acquisition_date is inside the lookback window. A retention/partition
-- policy is a separate, later decision - nothing here deletes data.
--
-- Stateless statements, idempotent, no data touched.
-- =====================================================================

create table if not exists public.ndvi_scene_evaluations (
    land_id              uuid        not null references public.lands(id) on delete cascade,
    tenant_id            uuid        not null,
    scene_id             text        not null,
    pipeline_version     text        not null,
    geometry_fingerprint text        not null,
    outcome              text        not null check (outcome in ('accepted', 'rejected')),
    reason               text,
    acquisition_date     date,
    evaluated_at         timestamptz not null default now(),
    primary key (land_id, scene_id, pipeline_version, geometry_fingerprint)
);

comment on table public.ndvi_scene_evaluations is
  'Satellite pipeline ledger of deterministic (land, scene) evaluations; lets the nightly run skip scenes it has already measured. Written only by the pipeline (service_role).';

create index if not exists ndvi_scene_evaluations_window_idx
    on public.ndvi_scene_evaluations (land_id, pipeline_version, acquisition_date);

-- Pipeline-internal: no farmer or admin UI reads it. RLS on with NO policies
-- means only service_role (which bypasses RLS) can read or write.
alter table public.ndvi_scene_evaluations enable row level security;
revoke all on public.ndvi_scene_evaluations from anon, authenticated;

-- ---------------------------------------------------------------------
-- VERIFY (read-only):
--   select count(*), count(distinct land_id), max(evaluated_at)
--     from public.ndvi_scene_evaluations;
-- after the first run it should hold roughly (lands x scenes evaluated).
-- ---------------------------------------------------------------------
