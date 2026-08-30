-- =====================================================================
-- v3 POST-RUN CLEANUP  (2026-08-30)
-- Run AFTER the successful v3 backfill (run 33286187042).
--
-- FINDING (verified, 6 rows total):
-- The v3 run wrote one row per PHYSICAL acquisition, choosing one MGRS
-- tile per (datetime, orbit). Pre-existing legacy rows for the SAME
-- land and SAME date came from the OTHER tile, so they carry a
-- different scene_id and were never overwritten by the upsert - the
-- unique key is (land_id, scene_id).
--
-- Result: 5 land/date pairs now hold two NDVI values that differ by
-- 0.029-0.250 (mean 0.137), the legacy one biased low by the F-1
-- zero-fill defect. Example - land 30197c15, 2026-08-21:
--     legacy T43QCU  ndvi 0.585  quality 0.912
--     v3     T43QDU  ndvi 0.726  quality 0.673
--
-- v_ndvi_decision_grade already hides them, so the chat brain is safe.
-- BUT weather/derive-pipeline.ts reads public.ndvi_data DIRECTLY via
-- NDVI_QUALITY_RESOLVER (quality >= 0.5). All 5 pass that filter, and
-- 3 are the newest optical row for their land - so the Kc / ETc /
-- root-depletion chain can still consume the wrong value.
--
-- This deletes ONLY legacy optical rows that a v3 row supersedes on the
-- same land and the same acquisition date. Nothing unique is lost: the
-- same physical acquisition survives, measured correctly.
-- =====================================================================

BEGIN;

-- Inspect before deleting (leave this SELECT in, it costs nothing).
CREATE TEMP TABLE _superseded AS
SELECT d.id, d.land_id, d.acquisition_date, d.scene_id,
       d.ndvi_value          AS legacy_ndvi,
       d.quality_score       AS legacy_quality,
       d.spatial_stat_method AS legacy_method,
       v3.ndvi_value         AS v3_ndvi,
       v3.scene_id           AS v3_scene
  FROM public.ndvi_data d
  JOIN public.ndvi_data v3
    ON v3.land_id = d.land_id
   AND v3.acquisition_date = d.acquisition_date
   AND v3.spatial_stat_method = 'fractional_coverage_v3'
   AND v3.id <> d.id
 WHERE d.spatial_stat_method <> 'fractional_coverage_v3'
   AND d.scene_id IS NOT NULL
   AND d.ndvi_value IS NOT NULL;

SELECT * FROM _superseded ORDER BY acquisition_date DESC;   -- expect 5 rows

DELETE FROM public.ndvi_data WHERE id IN (SELECT id FROM _superseded);

COMMIT;

-- =====================================================================
-- VERIFICATION (read-only)
-- =====================================================================
-- SELECT count(*) FROM ndvi_data WHERE scene_id IS NOT NULL AND ndvi_spatial_min = 0;
--   -- expect 0
--
-- SELECT land_id, acquisition_date, count(*)
--   FROM ndvi_data WHERE ndvi_value IS NOT NULL AND scene_id IS NOT NULL
--  GROUP BY 1,2 HAVING count(*) > 1;
--   -- expect 0 rows (one NDVI per land per acquisition date)
--
-- ALTER TABLE public.ndvi_data VALIDATE CONSTRAINT ndvi_spatial_support_plausible;
