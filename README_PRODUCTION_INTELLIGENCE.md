# Production Smallholder Satellite Intelligence

```text
Sentinel-2 / Sentinel-1
        |
        v
Measurement Engine
  calibration + masks + exact parcel coverage
  NDVI / NDRE(B8A) / NDMI / RVI
  spatial correlation support
        |
        v
Temporal Evidence
  own-history + gap-aware change
        |
        v
TATVA Reference Population
  same crop + sow/transplant week + block
  spatial ring only as secondary fallback
        |
        v
Evidence Engine
  uncertainty vector + multimodal agreement + provenance
        |
        v
ndvi_intelligence
        |
        v
TATVA / TARKA
        |
        v
Farmer API / App
```

`ndvi_data` remains the immutable observed measurement source. `ndvi_intelligence`
stores derived intelligence and never replaces the observation. Model-derived
values are blocked from farmer-facing decisions until independent validation
promotes the record to `validated`.
