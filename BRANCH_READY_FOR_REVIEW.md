# Review checkpoint

Branch: `feature/production-smallholder-intelligence`

This branch contains the production intelligence contract, evidence primitives,
SSOT configuration, validation-reference schema, regression tests, and a
fail-closed transformer for the existing measurement pipeline.

Before merge/backfill:
- verify GitHub Actions wiring result;
- review source diff;
- run complete test suite;
- quantify calibration-induced NDVI shift;
- verify B8A scene availability;
- do not enable estimated values or within-field zones prematurely.
