-- Production satellite-intelligence contract.
-- Apply through the project's Supabase migration workflow after review.

ALTER TABLE public.ndvi_intelligence
  ADD COLUMN IF NOT EXISTS observed_or_predicted text;

UPDATE public.ndvi_intelligence
SET intelligence_status = 'observed_context'
WHERE intelligence_status = 'observed_context_only';

ALTER TABLE public.ndvi_intelligence
  ALTER COLUMN intelligence_status SET DEFAULT 'observed_context';

DO $$ BEGIN
  ALTER TABLE public.ndvi_intelligence
    ADD CONSTRAINT ndvi_intelligence_observed_or_predicted_chk
    CHECK (observed_or_predicted IS NULL OR observed_or_predicted IN ('observed','predicted'));
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
  ALTER TABLE public.ndvi_intelligence
    ADD CONSTRAINT ndvi_intelligence_status_chk
    CHECK (intelligence_status IN ('observed_context','estimated_unvalidated','validated'));
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

CREATE TABLE IF NOT EXISTS public.satellite_intelligence_config (
  config_key text PRIMARY KEY,
  config_value jsonb NOT NULL,
  description text,
  source_reference text,
  is_active boolean NOT NULL DEFAULT true,
  updated_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE public.satellite_intelligence_config ENABLE ROW LEVEL SECURITY;

INSERT INTO public.satellite_intelligence_config(config_key, config_value, description)
VALUES
 ('minimum_stage_cohort_size','8','Minimum same-crop/sowing-week/block cohort before spatial-ring fallback is allowed'),
 ('minimum_zone_interior_cells','4','Minimum valid interior cells for within-field historical change evidence'),
 ('zone_min_quadrant_kappa','0.5','Minimum independent quadrant agreement before zones can become decision-visible'),
 ('zone_min_confirmed_cases','20','Minimum independent confirmed cases for zone validation gate'),
 ('temporal_support_gap_limit_days','30','Configuration value used to decay temporal support; not an agronomic threshold')
ON CONFLICT(config_key) DO NOTHING;

GRANT SELECT ON public.satellite_intelligence_config TO authenticated;

DO $$ BEGIN
  CREATE POLICY satellite_intelligence_config_read
    ON public.satellite_intelligence_config
    FOR SELECT TO authenticated USING (true);
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;
