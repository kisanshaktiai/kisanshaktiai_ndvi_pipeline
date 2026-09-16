-- Water intelligence is evidence, not diagnosis.
-- No binary water-stress decision is stored here. The layer stores observed
-- spectral signals so TATVA/TARKA can combine them with weather, phenology,
-- soil/root-zone state and validated reference data.

create table if not exists public.satellite_water_layers (
  id uuid primary key default gen_random_uuid(),
  tenant_id uuid not null,
  land_id uuid not null references public.lands(id) on delete cascade,
  scene_id text not null,
  acquisition_date date not null,
  acquisition_time timestamptz,
  layer_code text not null check (layer_code in ('surface_water_trace','canopy_moisture_signal')),
  value_mean numeric,
  value_median numeric,
  value_p10 numeric,
  value_p90 numeric,
  value_min numeric,
  value_max numeric,
  valid_fraction numeric,
  effective_pixel_count numeric,
  image_path text,
  image_metadata jsonb not null default '{}'::jsonb,
  uncertainty_json jsonb not null default '{}'::jsonb,
  evidence_json jsonb not null default '{}'::jsonb,
  provenance_json jsonb not null default '{}'::jsonb,
  status text not null default 'observed' check (status in ('observed','unavailable','invalid')),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint satellite_water_layers_unique unique (tenant_id, land_id, scene_id, layer_code)
);

create index if not exists satellite_water_layers_land_date_idx
  on public.satellite_water_layers (tenant_id, land_id, acquisition_date desc);

create index if not exists satellite_water_layers_scene_idx
  on public.satellite_water_layers (land_id, scene_id);

alter table public.satellite_water_layers enable row level security;

drop policy if exists satellite_water_layers_select on public.satellite_water_layers;
create policy satellite_water_layers_select
  on public.satellite_water_layers for select
  using (public.has_tenant_access(tenant_id));

drop policy if exists satellite_water_layers_insert on public.satellite_water_layers;
create policy satellite_water_layers_insert
  on public.satellite_water_layers for insert
  with check (public.has_tenant_access(tenant_id));

drop policy if exists satellite_water_layers_update on public.satellite_water_layers;
create policy satellite_water_layers_update
  on public.satellite_water_layers for update
  using (public.has_tenant_access(tenant_id))
  with check (public.has_tenant_access(tenant_id));

-- Presentation metadata is configuration, not agronomic decision logic.
create table if not exists public.satellite_layer_config (
  layer_code text primary key,
  value_min numeric not null,
  value_max numeric not null,
  color_stops jsonb not null,
  source text,
  enabled boolean not null default true,
  updated_at timestamptz not null default now(),
  constraint satellite_layer_config_range_chk check (value_max > value_min)
);

alter table public.satellite_layer_config enable row level security;
drop policy if exists satellite_layer_config_select on public.satellite_layer_config;
create policy satellite_layer_config_select
  on public.satellite_layer_config for select
  using (true);

-- Continuous spectral visualization only. There is deliberately NO universal
-- MNDWI threshold here: thresholding surface water from a single index is
-- site/scene dependent and must be validated before it becomes a rule.
insert into public.satellite_layer_config(layer_code,value_min,value_max,color_stops,source)
values
('surface_water_trace',-1,1,'[{"v":-1,"c":"#7f7f7f"},{"v":0,"c":"#c7d2d9"},{"v":0.2,"c":"#5dade2"},{"v":0.5,"c":"#21618c"},{"v":1,"c":"#0b3c5d"}]'::jsonb,'Presentation ramp only; no water-presence threshold'),
('canopy_moisture_signal',-1,1,'[{"v":-1,"c":"#8c6d5a"},{"v":-0.2,"c":"#c9a66b"},{"v":0,"c":"#d9e2b3"},{"v":0.3,"c":"#7fbf7b"},{"v":1,"c":"#216e39"}]'::jsonb,'Presentation ramp only; no water-stress threshold')
on conflict (layer_code) do nothing;

comment on table public.satellite_water_layers is 'Observed Sentinel-2 water-related spectral layers. Evidence only; no autonomous water-stress diagnosis.';
comment on column public.satellite_water_layers.evidence_json is 'Includes mask accounting, coverage, SCL fractions and spectral provenance.';
comment on column public.satellite_water_layers.uncertainty_json is 'Measurement support only; model_confidence remains null until validated.';
