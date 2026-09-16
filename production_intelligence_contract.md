# Production Smallholder Intelligence Contract

## Measurement invariants
- `ndvi_data.ndvi_value` is the observed Sentinel-2 parcel measurement and is never replaced by an estimate.
- Physical calibration is `raw * scale + offset` and calibration provenance is required in every optical observation.
- NDRE uses Sentinel-2 B8A and B05: `(B8A-B05)/(B8A+B05)`.
- Spatial uncertainty must distinguish Kish `n_eff` from spatially correlated `n_eff_spatial`.

## Evidence hierarchy
1. Parcel's own temporal history.
2. Same crop + same sowing/transplant week + same block cohort.
3. Same-scene spatial ring, secondary only.

No stage-relative interpretation is emitted when biological stage is unknown.

## Intelligence status
- `observed_context`: observed derived context only.
- `estimated_unvalidated`: model-derived estimate not eligible for farmer-facing decisions.
- `validated`: model-derived estimate that passed the configured independent validation gate.

## Uncertainty vector
Every intelligence record carries:
- `measurement_quality`
- `spatial_support`
- `temporal_support`
- `agreement`
- `model_confidence`

Decision gating uses the minimum applicable component; a scalar average must not replace these components.

## Spatial-zone gate
Within-field zones remain disabled until independent geotagged/quadrant validation reaches the configured agreement threshold and confirmed-case minimum. Sentinel-2 absolute pixels are not treated as sub-meter truth.

## Water-stress gate
NDMI is a canopy-water proxy. A farmer-facing water-stress interpretation requires agreement from NDMI decline, root-zone depletion, recent-rain condition, and water-sensitive biological stage; Landsat LST is corroborative block-scale evidence only.
