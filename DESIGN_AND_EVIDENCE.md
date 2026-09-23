# NDVI pipeline — the scalable model, built and measured
Repo `kisanshaktiai_ndvi_pipeline`, base `main @ cd1fd14` (restored). 2026-09-23.
**Measured** = run in this session. **Projected** = estimate, labelled as such.

## Is the current code working?
Yes. At today's 30 lands it runs in 2–5 minutes with correct values. Everything here is about growth — and one item (grouping) also helps today.

## The design: nightly work proportional to NEW satellite data, not to land count

```
PASS 1  stream light keys (id, tenant, centre) ── keyset pages, no geometry in memory
        └─ group by exact 100 km UTM cell from each land's own centre
PASS 2  per group: fetch full rows ─ one STAC search ─ O(N) grid plan into read-blocks (≤5 km, ≤200 lands)
        per block (bounded: ≤ 2×workers in flight):
          ledger: one query ─ skip lands whose every scene is already measured
          read each still-needed scene ONCE (padded 60 m for context) ─ budget-capped
          MEASURE each land from the block: statistics, parcel context, water   (no DB writes)
          PERSIST the block: observations → intelligence → snapshot(forward-only) → ledger → logs
```

## Measured
| What | Result |
|---|---|
| Block read vs per-parcel read (5 parcels, 8 bands, coverage, transform, dilated SCL) | 45 checks, 0 mismatches |
| Cloud inside a field / dilation across a boundary / shadow at block edge | identical SCL, masks, accept/reject, values |
| Parcel context from block (tiny parcel with widest ring, small, large, block edge) | every context value identical; reads 3 → 0 each |
| End-to-end 3 lands | identical; per-parcel reads 48 → 0 |
| Planner 1k / 10k / 100k parcels | 27 ms / 209 ms / 2.1 s — linear |
| **Your 30 real lands — grouping** | processed alone: **28 → 0**; STAC searches: **30 → 3**; 28 share one group |
| Orchestration on real centres | every land exactly once; concurrency ≤ workers |
| Night with nothing new | 0 reads, all `unchanged`, **exit 0 (healthy)** |
| Simulated STAC outage | **exit 2 (failure) — still detected** |
| New scene | read once; values identical to full re-measurement |
| Late older scene | stored; snapshot did not move back |
| Redrawn boundary | only that land re-evaluated, via block reads |
| One land's DB write fails | neighbours complete; failed land gets no snapshot, no `accepted` ledger entry |
| DB requests per 200-land block (derived from code paths) | ~1,400 → ~207 |
| Test suite | **61 passed** |

## Why no data is lost — every rule, where it is enforced
- `subset_band` reproduces `read_band` step for step, and **refuses** any window that leaves the block (then the canonical read is used).
- Ledger key = land × scene × pipeline_version × boundary fingerprint. Only deterministic outcomes are stored; transient errors retry; `accepted` is recorded only after the observation stored.
- Sentinel-1 fallback, land status and snapshot behave exactly as before incremental (explicit guards + tests).
- Kill switches, no deploy: `INCREMENTAL_SCENES=0`, `TILE_BATCH_READS=0`, `TILE_WORKERS`, `BLOCK_SPAN_M`, `BLOCK_MAX_LANDS`, `RASTER_CACHE_BUDGET_BYTES`; per run `--reprocess`.

## Projected at 1,000,000 lands (estimate)
Assumptions: ~500 × 100 km cells; ≤200 lands/block ⇒ ≥5,000 blocks; ~0.3 new S2 pass per cell per night.
- Optical + context reads/night ≈ 5,000 × 0.3 × 8 ≈ **12,000** (context now inside the block).
- Unchanged lands: zero reads, zero writes.
- Memory: set by `TILE_WORKERS` (bounded in-flight blocks + 384 MB raster budget), not by land count.

## Remaining, in order (not in this bundle)
1. **Sentinel-1 fallback** — still searches and reads radar per land, every night, for lands with no accepted optical in the window. In monsoon that is most lands: the largest remaining per-land cost. Needs S1 scene ledger + block reads.
2. **Land snapshot/status writes** — still one PATCH per changed land; batching needs a small DB function (migration).
3. **Water-layer writes** — still per scene per land.
4. **Execution model** — per-block work units on a queue, workers in Azure West Europe beside Planetary Computer; GitHub Actions for CI only (6-hour cap).

## Deploy
1. Push to a **branch**, run once (ledger table absent ⇒ no skipping, everything else active). Compare `ndvi_run_summary.notes.io`.
2. Approve `sql/2026-09-23_ndvi_scene_evaluations.sql` (not applied).
3. Two runs: night 1 fills the ledger; night 2 should show most lands `unchanged` and a large drop in `raster_reads`.
