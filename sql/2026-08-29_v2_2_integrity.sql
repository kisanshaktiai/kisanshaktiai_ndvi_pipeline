-- =====================================================================
-- NDVI pipeline v2.2 integrity migration  (forensic audit 2026-08-29)
-- Project: qfklkkzxemsbeniyugiz (shared KisanShakti DB)
-- Every statement is independent (no session state) and idempotent.
-- Apply AFTER reviewing; nothing here deletes observation rows.
-- =====================================================================

-- F-2  quality_score is real (float4), confidence_score is numeric.
--      float4(0.704) = 0.703999996 so an equal pair violated the 1e-9 check
--      and 3 of 8 optical lands failed every night. Tolerance 1e-6 covers
--      float4 rounding at 3 decimal places.
ALTER TABLE public.ndvi_data DROP CONSTRAINT IF EXISTS ndvi_confidence_le_quality;
ALTER TABLE public.ndvi_data ADD CONSTRAINT ndvi_confidence_le_quality
  CHECK (confidence_score IS NULL OR quality_score IS NULL
         OR confidence_score::double precision <= quality_score::double precision + 1e-6);

-- F-9  RVI dual-pol range is [0, 2], not [0, 1].
ALTER TABLE public.ndvi_data DROP CONSTRAINT IF EXISTS ndvi_rvi_range;
ALTER TABLE public.ndvi_data ADD CONSTRAINT ndvi_rvi_range
  CHECK (rvi_value IS NULL OR (rvi_value >= 0 AND rvi_value <= 2));

-- F-1  Pixel-count plausibility (v2.2.1). A field of A m2 holds at most
--      ceil(A/100 * 1.25) + 2 cells under centre sampling. Narrow strips
--      (< ~2 px wide) legitimately exceed that via the all_touched
--      fallback; the pipeline marks those rows metadata.footprint.
--      mixed_pixel_risk = true and caps confidence. Anything else above
--      the bound is the F-1 signature and is refused at the DB too.
ALTER TABLE public.ndvi_data DROP CONSTRAINT IF EXISTS ndvi_pixels_plausible;
ALTER TABLE public.ndvi_data ADD CONSTRAINT ndvi_pixels_plausible
  CHECK (total_pixels IS NULL OR field_area_m2 IS NULL OR field_area_m2 <= 0
         OR total_pixels <= ceil(field_area_m2 / 100.0 * 1.25) + 2
         OR coalesce((metadata->'footprint'->>'mixed_pixel_risk')::boolean, false)) NOT VALID;
-- NOT VALID: legacy v2.1 rows fail it. After the v2.2.1 --backfill run,
-- delete the provably corrupt leftovers (scenes outside the backfill
-- window that no run will ever overwrite), then validate:
--   DELETE FROM public.ndvi_data
--    WHERE observation_source = 'sentinel-2' AND scene_id IS NOT NULL
--      AND metadata->>'pipeline_version' = 'v2.1'
--      AND (ndvi_spatial_min = 0 OR total_pixels > ceil(field_area_m2/100.0*1.25)+2);
--   ALTER TABLE public.ndvi_data VALIDATE CONSTRAINT ndvi_pixels_plausible;

-- F-8/F-10 already handled in code (no schema impact).

-- Stale imagery: 24 lands show June-2026 thumbnails beside August metrics.
-- No image is produced by the runtime; remove the misleading references.
-- (Reversible: object names are the land ids in bucket ndvi-thumbnails.)
UPDATE public.lands
   SET ndvi_thumbnail_url = NULL, ndvi_geotiff_url = NULL
 WHERE (ndvi_thumbnail_url IS NOT NULL OR ndvi_geotiff_url IS NOT NULL);

-- F-3  Repair the 19 lands whose optical cache was NULLed by the radar path:
--      restore from their newest optical observation (if any exists).
UPDATE public.lands l
   SET last_ndvi_value       = d.ndvi_value,
       last_ndvi_calculation = d.acquisition_date,
       last_ndvi_quality     = d.quality_score,
       last_ndvi_source      = 'sentinel-2'
  FROM (
    SELECT DISTINCT ON (land_id) land_id, ndvi_value, acquisition_date, quality_score
      FROM public.ndvi_data
     WHERE observation_source = 'sentinel-2' AND observation_type = 'observed'
       AND ndvi_value IS NOT NULL AND scene_id IS NOT NULL
     ORDER BY land_id, acquisition_date DESC, acquisition_time DESC
  ) d
 WHERE d.land_id = l.id
   AND l.last_ndvi_value IS NULL
   AND l.last_ndvi_source = 'sentinel-1';

-- Index used by the new optical_history() and by v_ndvi_decision_grade.
CREATE INDEX IF NOT EXISTS idx_ndvi_data_land_source_acq
  ON public.ndvi_data (land_id, observation_source, acquisition_date DESC)
  WHERE ndvi_value IS NOT NULL;

-- Verification (read-only):
-- SELECT count(*) FROM ndvi_data WHERE observation_source='sentinel-2' AND scene_id IS NOT NULL
--   AND total_pixels > ceil(field_area_m2/100.0*1.25)+2;      -- must be 0 after backfill
-- SELECT count(*) FROM ndvi_data WHERE ndvi_spatial_min = 0 AND scene_id IS NOT NULL;  -- expect 0
-- SELECT ndvi_status, count(*) FROM lands WHERE is_active GROUP BY 1;  -- failed should drop to 0
