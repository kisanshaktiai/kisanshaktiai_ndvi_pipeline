-- =====================================================================
-- Scope trigger update_farmer_land_on_change to the columns it depends on.
-- Project: qfklkkzxemsbeniyugiz  (farmer-app domain; found during the NDVI
-- scalability audit)   STATUS: REVIEW BEFORE APPLYING.
--
-- FINDING (verified 2026-09-23 with pg_get_triggerdef / pg_get_functiondef):
--   the trigger fires on EVERY UPDATE of public.lands and runs
--   update_farmer_total_land_acres(): SUM(area_acres) over the farmer's lands
--   plus an UPDATE of public.farmers (updated_at = now()).
--   The NDVI pipeline updates lands.ndvi_status / last_ndvi_* nightly, so every
--   one of those writes also recomputes and rewrites the farmer row although
--   no area changed - up to ~1M pointless farmers writes per night at 1M lands.
--
-- Current definition (verified):
--   CREATE TRIGGER update_farmer_land_on_change AFTER INSERT OR DELETE OR UPDATE
--   ON public.lands FOR EACH ROW EXECUTE FUNCTION update_farmer_total_land_acres()
-- The function sums area_acres by farmer_id over ALL the farmer's lands, so an
-- UPDATE can change the total only when area_acres or farmer_id changes.
-- INSERT and DELETE are kept exactly as they are; only the UPDATE event is
-- narrowed. Timing (AFTER), level (FOR EACH ROW) and function are unchanged.
-- Stateless; function body unchanged; no data touched.
-- =====================================================================
drop trigger if exists update_farmer_land_on_change on public.lands;
create trigger update_farmer_land_on_change
  after insert or delete or update of area_acres, farmer_id on public.lands
  for each row
  execute function public.update_farmer_total_land_acres();

-- VERIFY (read-only):
--   select tgname, pg_get_triggerdef(oid) from pg_trigger
--    where tgrelid = 'public.lands'::regclass and tgname = 'update_farmer_land_on_change';
