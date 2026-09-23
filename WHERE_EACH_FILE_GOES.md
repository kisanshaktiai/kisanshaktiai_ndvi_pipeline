# Repo: kisanshaktiai_ndvi_pipeline — base main @ cd1fd14 — 2026-09-23 — push to a BRANCH first
| Repo path | New / changed | What |
|---|---|---|
| tile_reader.py | NEW | block read (60 m context pad, square corners) / exact subset with strict window guard / O(N) planner / scene-block filter / fingerprint / budget release |
| resource_budget.py | NEW | process-wide raster cache ceiling (data+mask), run counters, peak RSS |
| raster_utils.py | changed | read_band counts every remote read (all stages) |
| processor.py | changed | block-aware, exception-safe fallback, ledger-aware skipping, S1 behaviour preserved, band sharing |
| parcel_context.py | changed | context served from the block |
| context_enrichment.py | changed | passes blocks + ledger; failure reasons recorded |
| water_layer_runner.py | changed | reuses bands; only new scenes |
| tile_grouping.py | changed | exact spatial group key for every land (28/30 were processed alone) |
| db.py | changed | ledger read/write; keyset key streaming; per-group full fetch; batched history, observations, logs |
| ndvi_intelligence_writer.py | changed | batched observed-intelligence persistence with per-row isolation |
| main.py | changed | two-pass streaming; bounded in-flight blocks; measure → persist split; outage vs quiet-night guards; --reprocess |
| config.py | changed | TILE_WORKERS=4, TILE_BATCH_READS, BLOCK_SPAN_M, BLOCK_MAX_LANDS, INCREMENTAL_SCENES (all env) |
| .github/workflows/ndvi-pipeline.yml | changed | timeout 330 min |
| tests/test_v3_evidence.py | changed | 61 tests total |
| tests/fixture_real_land_centres.json | NEW | centres of the 30 live lands (ids truncated) for grouping tests |
| sql/2026-09-23_ndvi_scene_evaluations.sql | NEW | ledger table — **NOT applied, needs your approval** |
