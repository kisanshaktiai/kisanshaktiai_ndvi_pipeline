# KisanShakti NDVI pipeline (v2.2)

Nightly GitHub Actions job (`.github/workflows/ndvi-pipeline.yml`, 02:00 UTC) runs
`python main.py` and writes **one row per satellite acquisition per field** into
`public.ndvi_data` (unique on `land_id, scene_id`) of the shared Supabase project.

| Step | Module |
|---|---|
| Lands (paginated, `boundary_geom` PostGIS → GeoJSON) | `db.iter_lands` |
| −10 m erosion in local UTM, geometry confidence label | `raster_utils` |
| STAC search, Planetary Computer `sentinel-2-l2a` (no scene cloud filter > 98 %) | `sentinel_search`, `tile_grouping` |
| Tile-overlap dedupe (one row per physical acquisition) | `processor.dedupe_acquisitions` |
| Windowed COG read, reflectance offset, **exact per-cell coverage fraction** | `raster_utils.read_band` / `coverage_fractions` |
| SCL nearest resampling, cloud/shadow **dilated 2 px**, field-level gates | `raster_utils.scl_masks`, `quality.assess` |
| **Coverage-weighted** NDVI/SAVI/EVI/NDRE/MCARI/NDMI/NDWI/MNDWI + weighted histogram + cv | `indices.weighted_index_statistics` |
| **EPC, purity, boundary share, spatial SE, evidence tier** | `indices` + `quality.evidence_tier` |
| Area-identity check (EPC x 100 m2 = measured area), temporal-jump flag | `processor` |
| Sentinel-1 RTC RVI fallback (own column, never in `ndvi_value`) | `sar_vegetation` |
| Upsert, optical-only land cache, logs, run summary | `db`, `main` |

**Smallholder evidence model (v3).** Statistics are weighted by the exact
fraction of each 10 m cell inside the farmer's polygon, so a 10-guntha
(~1012 m2) field yields an area-true mean with an explicit Effective Pixel
Count (EPC ~= area/100), purity, spatial standard error and evidence tier.
Fixed -10 m erosion applies only to fields >= 0.5 ha (on a square 10-guntha
field it would delete ~86 % of the farm). EPC >= 8 is the published support
anchor (Sitokonstantinou et al. 2020, Remote Sensing 12(14):2195); the 5 and
3 boundaries are engineering rules, not validated constants. Every row
carries `spatial_stat_method` so v2 and v3 values are never compared blindly
in one trend.

The pipeline stores **physics only**. Agronomic interpretation (stage-relative
NDVI, health classes, trends for advisory) is the decision layer's job and must
read `v_ndvi_decision_grade` (optical, quality ≥ 0.55, freshness) — never raw
`ndvi_data` ordered by `date`, which interleaves radar rows with `ndvi_value NULL`.

Migrations: apply `sql/2026-08-29_v2_2_integrity.sql` then
`sql/2026-08-30_v3_evidence_model.sql`. The code is safe to deploy before
either — `db.py` probes the live schema and keeps unmigrated evidence fields
in `metadata.evidence`.

Run options: `python main.py [--tenant UUID] [--lookback N] [--backfill] [--dry-run]`
Exit 2 = zero acquisitions accepted. Apply `sql/2026-08-29_v2_2_integrity.sql` before the first v2.2 run, then run with `--backfill` once.
