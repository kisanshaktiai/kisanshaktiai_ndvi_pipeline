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
| Windowed COG read, reflectance offset, **10 m footprint from rasterio mask** | `raster_utils.read_band` |
| SCL nearest resampling, cloud/shadow **dilated 2 px**, field-level gates | `raster_utils.scl_masks`, `quality.assess` |
| NDVI/SAVI/EVI/NDRE/MCARI/NDMI/NDWI/MNDWI + histogram + cv | `indices` |
| Pixel-count plausibility gate, temporal-jump flag | `processor` |
| Sentinel-1 RTC RVI fallback (own column, never in `ndvi_value`) | `sar_vegetation` |
| Upsert, optical-only land cache, logs, run summary | `db`, `main` |

The pipeline stores **physics only**. Agronomic interpretation (stage-relative
NDVI, health classes, trends for advisory) is the decision layer's job and must
read `v_ndvi_decision_grade` (optical, quality ≥ 0.55, freshness) — never raw
`ndvi_data` ordered by `date`, which interleaves radar rows with `ndvi_value NULL`.

Run options: `python main.py [--tenant UUID] [--lookback N] [--backfill] [--dry-run]`
Exit 2 = zero acquisitions accepted. Apply `sql/2026-08-29_v2_2_integrity.sql` before the first v2.2 run, then run with `--backfill` once.
