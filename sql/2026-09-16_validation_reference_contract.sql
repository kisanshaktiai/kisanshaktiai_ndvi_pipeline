-- Independent validation datasets are deliberately separate from ndvi_data.
-- Do not populate these tables from Sentinel-2 values used by the model.

CREATE TABLE IF NOT EXISTS public.satellite_validation_reference (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL,
  land_id uuid NOT NULL,
  observation_date date NOT NULL,
  reference_type text NOT NULL,
  reference_value numeric,
  reference_payload jsonb NOT NULL DEFAULT '{}'::jsonb,
  collector text,
  dataset_version text NOT NULL,
  is_independent boolean NOT NULL DEFAULT true,
  created_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT satellite_validation_reference_type_chk
    CHECK (reference_type IN ('geotagged_photo','kvk_quadrant','planetscope','field_measurement')),
  CONSTRAINT satellite_validation_reference_independence_chk
    CHECK (is_independent = true)
);

CREATE INDEX IF NOT EXISTS idx_satellite_validation_reference_land_date
  ON public.satellite_validation_reference(land_id, observation_date DESC);

CREATE INDEX IF NOT EXISTS idx_satellite_validation_reference_dataset
  ON public.satellite_validation_reference(dataset_version, reference_type);

ALTER TABLE public.satellite_validation_reference ENABLE ROW LEVEL SECURITY;

DO $$ BEGIN
  CREATE POLICY satellite_validation_reference_tenant_read
    ON public.satellite_validation_reference
    FOR SELECT TO authenticated
    USING (tenant_id = (select auth.uid()));
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;
