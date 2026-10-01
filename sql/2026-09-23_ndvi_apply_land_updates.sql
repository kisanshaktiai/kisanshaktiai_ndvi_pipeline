-- =====================================================================
-- ndvi_apply_land_updates(jsonb): a whole read-block's land snapshot and
-- status changes in ONE set-based UPDATE, instead of one HTTP request per
-- land (release review FIX-1).
-- Project: qfklkkzxemsbeniyugiz   Repo: kisanshaktiai_ndvi_pipeline
-- STATUS: REVIEW BEFORE APPLYING. The pipeline works WITHOUT it: if the
-- function is missing it falls back to the existing per-land updates.
--
-- Input: a JSON array, ONE entry per land (the pipeline merges a land's
-- snapshot and status into a single entry, so the UPDATE is deterministic):
--   id                      uuid     (required)
--   has_snapshot            boolean  set the optical snapshot fields
--   last_ndvi_value         numeric
--   last_ndvi_calculation   date     TRUE acquisition date
--   last_ndvi_quality       numeric
--   last_ndvi_source        text
--   ndvi_status             text     (null = keep)
--   set_note                boolean  write ndvi_status_note (status updates)
--   ndvi_status_note        text
--   ndvi_thumbnail_url      text     (null = keep)
-- Semantics are exactly those of db.update_land_snapshot / db.mark_land_status.
-- Returns the number of lands updated. Stateless, idempotent, no data deleted.
-- =====================================================================
create or replace function public.ndvi_apply_land_updates(p_updates jsonb)
returns integer
language plpgsql
security invoker
set search_path = public
as $$
declare
  n integer;
begin
  with u as (
    select * from jsonb_to_recordset(p_updates) as x(
      id uuid, has_snapshot boolean,
      last_ndvi_value numeric, last_ndvi_calculation date, last_ndvi_quality numeric,
      last_ndvi_source text, ndvi_status text, set_note boolean, ndvi_status_note text,
      ndvi_thumbnail_url text)
  )
  update public.lands l set
    last_ndvi_value       = case when u.has_snapshot then u.last_ndvi_value       else l.last_ndvi_value end,
    last_ndvi_calculation = case when u.has_snapshot then u.last_ndvi_calculation else l.last_ndvi_calculation end,
    last_ndvi_quality     = case when u.has_snapshot then u.last_ndvi_quality     else l.last_ndvi_quality end,
    last_ndvi_source      = case when u.has_snapshot then u.last_ndvi_source      else l.last_ndvi_source end,
    ndvi_tested           = case when u.has_snapshot then true                    else l.ndvi_tested end,
    ndvi_status           = coalesce(u.ndvi_status, l.ndvi_status),
    ndvi_status_note      = case when coalesce(u.set_note, false) then u.ndvi_status_note else l.ndvi_status_note end,
    ndvi_thumbnail_url    = coalesce(u.ndvi_thumbnail_url, l.ndvi_thumbnail_url),
    last_processed_at     = now(),
    updated_at            = case when u.has_snapshot then now() else l.updated_at end
  from u
  where l.id = u.id;
  get diagnostics n = row_count;
  return n;
end;
$$;

comment on function public.ndvi_apply_land_updates(jsonb) is
  'Satellite pipeline: batched land snapshot/status updates (one entry per land). Service role only.';

revoke all on function public.ndvi_apply_land_updates(jsonb) from public, anon, authenticated;
grant execute on function public.ndvi_apply_land_updates(jsonb) to service_role;

-- VERIFY (read-only):
--   select proname, prosecdef from pg_proc where proname = 'ndvi_apply_land_updates';
