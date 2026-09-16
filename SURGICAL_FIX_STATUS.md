# Surgical Fix Status — 2026-09-16

## Implemented on branch
- Production evidence primitives: calibration provenance, canonical B8A/B05 NDRE helper, empirical-variogram spatial support, uncertainty vector, cohort key/fallback policy, water-stress evidence gate, and spatial-zone validation gate.
- Production intelligence contract documenting observed-vs-estimated separation and evidence hierarchy.
- `ndvi_intelligence.observed_or_predicted` and normalized intelligence statuses.
- `satellite_intelligence_config` SSOT table with cohort, spatial-zone and temporal-support configuration.
- Fail-closed source transformer for wiring B8A, calibration provenance and spatial support into the existing processor.
- Regression tests for the new evidence contract.

## Deliberately not claimed complete
- The transformer has not been confirmed by a successful GitHub Actions run yet.
- No NDVI historical backfill has been executed.
- No downstream threshold retuning has been performed.
- No ML-estimated NDVI is enabled for farmer-facing decisions.
- No within-field zone is decision-visible until independent validation passes the configured gate.
- Stage-relative cohort integration still requires a verified production block identifier; the current `lands` schema does not expose a dedicated block_id, so taluka/village is not silently substituted.
- Sentinel-1 all-pass optical/radar agreement and conformal S1→NDVI model remain a subsequent implementation phase.

## Required verification sequence
1. Confirm source-transform workflow result and run full test suite.
2. Inspect pre/post calibration distributions on a controlled backfill sample.
3. Quantify NDVI shifts before changing agronomic thresholds.
4. Verify B8A availability and NDRE output on real Sentinel-2 scenes.
5. Verify spatial-support metadata on smallholder parcels.
6. Implement TATVA stage-cohort query once block identity SSOT is confirmed.
7. Build temporal cube and validation datasets before enabling model estimates.
