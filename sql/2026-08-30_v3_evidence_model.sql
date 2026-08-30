-- =====================================================================
-- v3 SMALLHOLDER EVIDENCE MODEL  (2026-08-30, rev 3)
-- Project: qfklkkzxemsbeniyugiz
--
-- REV 2 FIXES THE FAILURE OF REV 1:
--   ERROR 23514: new row for relation "ndvi_data" violates check
--   constraint "ndvi_pixels_plausible"
--
-- Cause (verified, not guessed): ndvi_pixels_plausible was added by the
-- 2026-08-29 migration as NOT VALID. NOT VALID only skips the one-time
-- scan of EXISTING rows - PostgreSQL still enforces the constraint on
-- every subsequent INSERT *and UPDATE*. Rev 1's provenance backfill
--     UPDATE ndvi_data SET spatial_stat_method = ...
-- rewrote every row, so each one was re-checked, and the 9 legacy v2.1
-- rows carrying the original F-1 defect (total_pixels far above what the
-- field area allows, e.g. land 6e06302e: 28 cells on 1017.7 m2 where the
-- bound is 15) failed. The whole migration rolled back - verified: 0 of
-- the 7 v3 columns exist, so this file is safe to re-run from scratch.
--
-- Second, deeper problem rev 1 exposed: ndvi_pixels_plausible encodes a
-- v2.2 assumption (whole-cell counting, so cells <= area/100*1.25+2).
-- That bound is WRONG for v3. Under fractional coverage, all_touched
-- deliberately selects every cell the polygon touches - on a narrow strip
-- that is legitimately ~2x the area-based count - and the coverage
-- weights, not the cell count, carry the correctness. The invariant that
-- actually holds for v3 is on EFFECTIVE pixels:
--        effective_pixel_count * 100 m2  <=  field area
-- So the constraint is replaced with a METHOD-AWARE one: each row is
-- judged by the rule of the method that produced it.
--
-- Dry-run against live data before writing this file: 0 of 2,579 rows
-- fail the new constraint (v2.2=26, v2.1=72, legacy_v1=2,481).
--
-- REV 3 FIXES THE SECOND FAILURE:
--   ERROR 42P16: cannot change name of view column "age_days" to
--   "effective_pixel_count"
--
-- Cause: CREATE OR REPLACE VIEW may only APPEND columns at the end of the
-- existing column list - it cannot insert new ones in the middle or
-- reorder them. The live view ends with (... observation_source, age_days,
-- is_fresh, recency_rank) and rev 2 inserted six evidence columns before
-- age_days, so Postgres read that as renaming column 17.
--
-- Fix: DROP then CREATE. Verified safe before writing this file - the view
-- has no dependent views, no dependent functions and is referenced by no
-- RLS policy. The DROP is deliberately NOT cascaded: if anything unknown
-- does depend on it, the DROP fails and the whole transaction rolls back
-- rather than silently destroying it.
--
-- A DROP discards the view's grants, so they are restored explicitly
-- below from the live catalogue (anon, authenticated, service_role,
-- postgres) along with security_invoker=true.
--
-- PREREQUISITE: 2026-08-29_v2_2_integrity.sql - VERIFIED APPLIED
-- (confidence epsilon is 1e-6, thumbnail URLs cleared).
-- =====================================================================

BEGIN;

-- ---------------------------------------------------------------------
-- 1. EVIDENCE COLUMNS
--    ADD COLUMN without a default does not rewrite rows and does not
--    re-check constraints, so this step is safe on its own.
-- ---------------------------------------------------------------------
ALTER TABLE public.ndvi_data
  ADD COLUMN IF NOT EXISTS effective_pixel_count            numeric,
  ADD COLUMN IF NOT EXISTS coverage_weighted_purity         numeric,
  ADD COLUMN IF NOT EXISTS boundary_contamination_fraction  numeric,
  ADD COLUMN IF NOT EXISTS ndvi_spatial_se                  numeric,
  ADD COLUMN IF NOT EXISTS evidence_confidence              text,
  ADD COLUMN IF NOT EXISTS measurement_status               text,
  ADD COLUMN IF NOT EXISTS spatial_stat_method              text;

COMMENT ON COLUMN public.ndvi_data.effective_pixel_count IS
  'Sum of per-cell polygon coverage fractions behind the statistic. EPC*100 m2 = measured area. NOT a cell count.';
COMMENT ON COLUMN public.ndvi_data.coverage_weighted_purity IS
  'EPC / contributing cell count. 1.0 = every cell wholly inside the field; low = the measurement rests on boundary cells shared with bunds, roads or neighbours.';
COMMENT ON COLUMN public.ndvi_data.boundary_contamination_fraction IS
  'Share of EPC contributed by cells that are only partly inside the field.';
COMMENT ON COLUMN public.ndvi_data.ndvi_spatial_se IS
  'Standard error of the coverage-weighted mean from SPATIAL SAMPLING ONLY. Excludes sensor, atmospheric-correction, geolocation and boundary-delineation error. A floor on uncertainty, never the whole of it.';
COMMENT ON COLUMN public.ndvi_data.evidence_confidence IS
  'high | medium | low | insufficient, from the EPC band. EPC>=8 is the published anchor (Sitokonstantinou et al. 2020, Remote Sensing 12(14):2195); the 5 and 3 boundaries are engineering rules, NOT validated constants.';
COMMENT ON COLUMN public.ndvi_data.spatial_stat_method IS
  'Provenance. fractional_coverage_v3 = coverage-weighted (area-true). pixel_mask_v2_2 = unweighted whole-cell mean. pixel_mask_v2_1 = same, plus the F-1 zero-fill defect. Rows of different methods must not be compared blindly inside one trend.';

-- ---------------------------------------------------------------------
-- 2. REPLACE THE CONSTRAINT *BEFORE* TOUCHING ANY ROW
--    This ordering is the actual fix: the backfill in step 3 rewrites
--    every row and would re-trigger the old rule otherwise.
-- ---------------------------------------------------------------------
ALTER TABLE public.ndvi_data DROP CONSTRAINT IF EXISTS ndvi_pixels_plausible;

ALTER TABLE public.ndvi_data ADD CONSTRAINT ndvi_spatial_support_plausible CHECK (
  CASE
    -- v3: the area identity. EPC can never exceed the field it measures.
    -- 2 % + 1 cell of slack absorbs polygon/raster edge rounding.
    WHEN spatial_stat_method = 'fractional_coverage_v3' THEN
      effective_pixel_count IS NULL
      OR field_area_m2 IS NULL OR field_area_m2 <= 0
      OR effective_pixel_count * 100.0 <= field_area_m2 * 1.02 + 100.0

    -- v2.2: whole-cell counting, so the original area bound still applies.
    WHEN spatial_stat_method = 'pixel_mask_v2_2' THEN
      total_pixels IS NULL
      OR field_area_m2 IS NULL OR field_area_m2 <= 0
      OR total_pixels <= ceil(field_area_m2 / 100.0 * 1.25) + 2
      OR COALESCE((metadata->'footprint'->>'mixed_pixel_risk')::boolean, false)

    -- Legacy rows are known-defective, are labelled as such, and are
    -- excluded from the decision view below. Holding them to a rule they
    -- were never written under would only block maintenance UPDATEs -
    -- which is exactly what broke rev 1.
    ELSE true
  END
) NOT VALID;

-- ---------------------------------------------------------------------
-- 3. PROVENANCE BACKFILL (now passes: each row is judged by its own rule)
-- ---------------------------------------------------------------------
UPDATE public.ndvi_data
   SET spatial_stat_method = CASE
         WHEN metadata->>'pipeline_version' LIKE 'v3%'    THEN 'fractional_coverage_v3'
         WHEN metadata->>'pipeline_version' LIKE 'v2.2%'  THEN 'pixel_mask_v2_2'
         WHEN scene_id IS NOT NULL                        THEN 'pixel_mask_v2_1'
         ELSE 'legacy_v1' END
 WHERE spatial_stat_method IS NULL;

-- Every row now carries a method, so the constraint can be proven.
ALTER TABLE public.ndvi_data VALIDATE CONSTRAINT ndvi_spatial_support_plausible;

CREATE INDEX IF NOT EXISTS idx_ndvi_data_evidence
  ON public.ndvi_data (land_id, acquisition_date DESC)
  WHERE ndvi_value IS NOT NULL AND effective_pixel_count IS NOT NULL;

-- ---------------------------------------------------------------------
-- 4. DECISION-GRADE VIEW
--    Adds the evidence columns, and excludes rows carrying the F-1
--    fingerprint (zero-fill pixels inside the field, or a cell count the
--    field area cannot hold). Those are the 9 v2.1 rows whose NDVI is
--    biased 25-60 % low.
--
--    IMPACT, measured before writing this file: the view goes from 17
--    rows / 11 lands to 8 rows / 7 lands. Four lands lose all
--    decision-grade NDVI until the v3 --backfill run repopulates them.
--    That is the intended outcome: no NDVI is safer for a farmer than an
--    NDVI biased low enough to fire a false stress alert.
-- ---------------------------------------------------------------------
-- Column list changes shape, so REPLACE is not possible: drop first.
DROP VIEW IF EXISTS public.v_ndvi_decision_grade;

CREATE VIEW public.v_ndvi_decision_grade
WITH (security_invoker = true) AS
SELECT land_id, tenant_id, acquisition_date, acquisition_time, scene_id,
       ndvi_value, savi_value, ndre_value, ndmi_value, mcari_value,
       ndvi_spatial_std, uniformity_cv, quality_score, confidence_level,
       cloud_cover, observation_source,
       effective_pixel_count,
       coverage_weighted_purity,
       boundary_contamination_fraction,
       ndvi_spatial_se,
       COALESCE(evidence_confidence,
                CASE WHEN effective_pixel_count IS NULL THEN 'unrated' END) AS evidence_confidence,
       COALESCE(measurement_status,
                CASE WHEN effective_pixel_count IS NULL THEN 'PRE_V3_NO_EPC' END) AS measurement_status,
       COALESCE(spatial_stat_method, 'pixel_mask_legacy') AS spatial_stat_method,
       CURRENT_DATE - acquisition_date AS age_days,
       (CURRENT_DATE - acquisition_date) <= 14 AS is_fresh,
       row_number() OVER (PARTITION BY land_id ORDER BY acquisition_date DESC) AS recency_rank
  FROM ndvi_data n
 WHERE observation_type = 'observed'::ndvi_observation_type
   AND is_interpolated = false
   AND ndvi_value IS NOT NULL
   AND quality_score >= 0.55
   -- v3 rows must clear the spatial-support floor; pre-v3 rows pass
   -- through unrated rather than every land going dark on deploy.
   AND (effective_pixel_count IS NULL OR effective_pixel_count >= 3.0)
   -- F-1 fingerprint: out-of-polygon fill counted as bare soil.
   AND COALESCE(ndvi_spatial_min, 1) <> 0
   AND NOT (field_area_m2 IS NOT NULL AND field_area_m2 > 0
            AND total_pixels > ceil(field_area_m2 / 100.0 * 1.25) + 2
            AND COALESCE((metadata->'footprint'->>'mixed_pixel_risk')::boolean, false) = false
            AND COALESCE(spatial_stat_method, '') <> 'fractional_coverage_v3');

-- Restore the grants the DROP removed, exactly as they were on the live
-- object. security_invoker=true (set above) means each caller is still
-- filtered by their own RLS on ndvi_data - these grants do not widen
-- tenant access.
ALTER VIEW public.v_ndvi_decision_grade OWNER TO postgres;
GRANT ALL ON TABLE public.v_ndvi_decision_grade TO anon;
GRANT ALL ON TABLE public.v_ndvi_decision_grade TO authenticated;
GRANT ALL ON TABLE public.v_ndvi_decision_grade TO service_role;
GRANT ALL ON TABLE public.v_ndvi_decision_grade TO postgres;

COMMIT;

-- =====================================================================
-- VERIFICATION (read-only, run after COMMIT)
-- =====================================================================
-- SELECT spatial_stat_method, count(*), round(avg(effective_pixel_count),2) AS avg_epc
--   FROM ndvi_data GROUP BY 1 ORDER BY 2 DESC;
--   -- expect: legacy_v1 2481, pixel_mask_v2_1 72, pixel_mask_v2_2 26
--
-- SELECT count(*) AS rows, count(DISTINCT land_id) AS lands FROM v_ndvi_decision_grade;
--   -- expect 8 rows / 7 lands immediately after this migration,
--   -- rising again after the v3 backfill run
--
-- SELECT grantee, privilege_type FROM information_schema.role_table_grants
--  WHERE table_name = 'v_ndvi_decision_grade' ORDER BY 1,2;
--   -- expect anon, authenticated, postgres, service_role (grants restored)
--
-- SELECT reloptions FROM pg_class WHERE oid = 'public.v_ndvi_decision_grade'::regclass;
--   -- expect {security_invoker=true}
--
-- After deploying v3 and running the workflow with backfill = true:
-- SELECT land_id, effective_pixel_count, coverage_weighted_purity,
--        evidence_confidence, ndvi_spatial_se
--   FROM v_ndvi_decision_grade WHERE recency_rank = 1
--   ORDER BY effective_pixel_count;
--   -- every v3 row must satisfy EPC*100 <= field_area_m2 * 1.02 + 100
