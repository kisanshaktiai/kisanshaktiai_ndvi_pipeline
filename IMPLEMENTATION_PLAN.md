# Production Implementation Sequence

1. Measurement integrity
   - Verify the fail-closed transformer applies B8A, calibration provenance and spatial-support evidence.
   - Run full test suite and CI.
   - Compare pre/post calibration NDVI distributions on a controlled sample.
   - Only after comparison, evaluate downstream threshold impact.

2. TATVA cohort intelligence
   - Resolve the canonical block identifier from the existing location ontology/SSOT.
   - Build cohort query: crop + sowing/transplant ISO week + block.
   - Use the configured minimum cohort size; ring is secondary fallback.

3. Temporal cube
   - Preserve per-acquisition observations.
   - Add Whittaker smoothing with gap metadata, own-history rate-of-change, cohort deviation and days-since-clear.
   - Add same-cell historical change only when >= configured interior support.

4. Multimodal evidence
   - Compute Sentinel-1 RVI on every suitable pass.
   - Add optical/radar agreement.
   - Keep S1→NDVI estimates disabled until independent calibration and conformal validation exist.

5. Water evidence
   - Combine NDMI trajectory with land_weather_state root-zone depletion, recent rainfall and crop-stage sensitivity.
   - Use Landsat LST only as block-scale corroboration.

6. Validation
   - Populate independent geotagged-photo, KVK-quadrant and PlanetScope references.
   - Stratify validation by crop, stage, parcel size, season and zone size.
   - Keep `estimated_unvalidated` out of farmer decisions.

7. Farmer/API integration
   - App consumes canonical intelligence API only.
   - No client-side agronomic thresholds, prediction or recommendation logic.
