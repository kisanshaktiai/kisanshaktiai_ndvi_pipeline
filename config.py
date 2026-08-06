"""
config.py - NDVI pipeline v2 configuration (SINGLE SOURCE OF TRUTH)

v1 DEFECT FIXED (P-05): config.VALID_SCL existed but raster_utils.py defined its
own broader list and never imported this file. In v2 every module imports from
here; no module defines a local copy.
"""

# ---------------------------------------------------------------------------
# TEMPORAL WINDOW
# ---------------------------------------------------------------------------
# v1 used 15 days and collapsed everything inside it into ONE row (P-02).
# v2 uses the window only to bound the STAC query; each acquisition inside it
# becomes its OWN row stamped with its OWN true acquisition datetime.
LOOKBACK_DAYS = 20          # ~6 acquisitions at 2.5-3d revisit (3-sat constellation)
S1_LOOKBACK_DAYS = 24
BACKFILL_DAYS = 120         # used only by the historical backfill entrypoint

# ---------------------------------------------------------------------------
# COLLECTIONS (Microsoft Planetary Computer)
# ---------------------------------------------------------------------------
S2_COLLECTION = "sentinel-2-l2a"
S1_RTC_COLLECTION = "sentinel-1-rtc"    # radiometrically terrain-corrected gamma0
S1_GRD_COLLECTION = "sentinel-1-grd"    # fallback if RTC unavailable
STAC_URL = "https://planetarycomputer.microsoft.com/api/stac/v1"

# ---------------------------------------------------------------------------
# CLOUD HANDLING   <<< THE FIX THAT ENDS MONSOON BLINDNESS (P-14) >>>
# ---------------------------------------------------------------------------
# v1: STAC query filtered eo:cloud_cover < 30.
#     eo:cloud_cover describes the ENTIRE 290x110 km granule. During the
#     Maharashtra monsoon essentially no granule is below 30%, so the search
#     returned ZERO items and 100% of lands were skipped - while the
#     satellites were in fact imaging every field every ~3 days.
#
# v2: do NOT pre-filter on scene cloud. Retrieve every acquisition, then decide
#     per-field from the SCL mask over the actual polygon. A granule at 70%
#     cloud is frequently clear over one 2-hectare farm.
#
# Only fully opaque scenes are worth rejecting up front: they cost bandwidth
# and can never yield a valid pixel.
SCENE_CLOUD_REJECT_ABOVE = 98.0   # percent

# FIELD-LEVEL cloud rejection. THIS is "reject scenes exceeding the configured
# cloud threshold" implemented correctly.
#
# The threshold must be evaluated over the FIELD, not the granule. v1 applied
# it to eo:cloud_cover - a property of a 290 x 110 km scene - and produced a
# 100% skip rate for 29 consecutive days. A granule at 70% cloud is routinely
# clear over one 2-hectare farm; a granule at 10% cloud can be entirely
# obscured over another.
MAX_FIELD_CLOUD_FRACTION = 0.30     # >30% of the FIELD under cloud -> reject
MAX_FIELD_SHADOW_FRACTION = 0.20
MAX_FIELD_SNOW_FRACTION = 0.10

# ---------------------------------------------------------------------------
# SENTINEL-2 SCENE CLASSIFICATION LAYER (SCL)
# ---------------------------------------------------------------------------
#  0 no data          1 saturated/defective   2 dark area / topo shadow
#  3 CLOUD SHADOW     4 VEGETATION            5 NOT VEGETATED (bare soil)
#  6 WATER            7 UNCLASSIFIED          8 CLOUD medium probability
#  9 CLOUD high prob 10 THIN CIRRUS          11 SNOW / ICE
#
# v1 used [4, 5, 6, 7] - admitting WATER and UNCLASSIFIED into the crop
# average (P-05). Water has NDVI ~ -0.3..0.0 and drags the field mean down;
# catastrophic for paddy, where standing water is normal.
SCL_CROP_SURFACE = [4, 5]           # the only valid crop-canopy classes
SCL_CLOUD        = [8, 9, 10]
SCL_SHADOW       = [2, 3]
SCL_WATER        = [6]              # tracked separately (paddy / flood signal)
SCL_INVALID      = [0, 7]           # no-data, unclassified
SCL_SATURATED    = [1]              # saturated / defective sensor pixel
SCL_SNOW         = [11]             # snow / ice
SCL_DARK         = [2]              # dark area / topographic shadow

# ---------------------------------------------------------------------------
# GEOMETRY / MIXED-PIXEL CONTROL  (P-12)
# ---------------------------------------------------------------------------
# Sentinel-2's point spread function draws signal from beyond the nominal 10 m
# footprint. Without erosion, boundary pixels mix crop with bunds, tracks,
# margins and neighbouring crops. 29% of this tenant's fields are under
# 0.5 acre, where nearly every pixel is an edge pixel.
FIELD_BUFFER_M = -10.0              # one S2 pixel inward
MIN_BUFFERED_AREA_M2 = 400.0        # below this, erosion would erase the field

# ---------------------------------------------------------------------------
# ACCEPTANCE THRESHOLDS  (P-13, C-13)
# ---------------------------------------------------------------------------
# v1 accepted an acquisition on >= 4 valid pixels with no coverage floor, then
# stored it indistinguishably from a 2000-pixel observation.
# REVISED after the 2026-08-06 19:29 run. The original value of 12 was an
# absolute floor applied to every field regardless of size, and it excluded
# this tenant's smallholdings BY GEOMETRY, not by data quality:
#
#   accepted lands : 0.42 - 56.84 acre, radar valid_pixels 13 .. 2096
#   skipped lands  : 0.23 -  1.00 acre, radar valid_pixels 10 / 11 (8 lands)
#                                                            6      (1 land)
#   3 of the 9 skipped can NEVER reach 12 pixels - a 0.23-acre field is
#   ~930 m2 = ~9 Sentinel pixels in total. The threshold was structurally
#   impossible for them.
#
# 12 sat exactly on the dividing line: the smallest ACCEPTED field had 13.
# That is a threshold artefact, not a quality boundary.
#
# 8 is the new floor. Rationale: SAR speckle needs a minimum number of
# independent looks for a stable mean; below ~8 the estimate is dominated by
# speckle and edge mixing. 8 recovers 8 of the 9 skipped lands and still
# refuses the 6-pixel case, which is genuinely too small to average.
#
# Fields between 8 and ~20 pixels are NOT treated as equal to large fields:
# MICRO_LAND_FACTOR already discounts confidence by 0.65 and flags
# micro_land=true, so a small-field observation is stored honestly rather
# than either discarded or overstated.
MIN_VALID_PIXELS = 8
MIN_VALID_FRACTION = 0.50           # >=50% of the BUFFERED field must be clean
MIN_QUALITY_SCORE = 0.35            # below this the row is rejected outright

QW_COVERAGE = 0.50                  # quality weights, must sum to 1.0
QW_PIXELCOUNT = 0.20
QW_CLOUDFREE = 0.30
QUALITY_SATURATION_PIXELS = 50

# ---------------------------------------------------------------------------
# SENTINEL-1 MONSOON FALLBACK  (P-16)
# ---------------------------------------------------------------------------
# Radar penetrates cloud. v1 already fetched S1 nightly and fed it to an
# uncalibrated ad-hoc formula that never once produced a value (0 rows).
# v2 computes the Radar Vegetation Index from calibrated gamma0 into its OWN
# column - never mixed into ndvi_value.
ENABLE_S1_FALLBACK = True
S1_MIN_VALID_PIXELS = 12

# ---------------------------------------------------------------------------
# SCALE / CONCURRENCY  (P-08)
# ---------------------------------------------------------------------------
# v1: fetch_lands(limit=100), no ORDER BY, no pagination -> a permanent ceiling
# of 100 arbitrary lands, while reporting success.
LAND_PAGE_SIZE = 500
MAX_LANDS_PER_RUN = None            # None == process every eligible land
TILE_WORKERS = 4
SCENE_WORKERS = 2

# ---------------------------------------------------------------------------
# NUMERIC
# ---------------------------------------------------------------------------
NDVI_DECIMALS = 4
EPS = 1e-6

# ---------------------------------------------------------------------------
# SMALLHOLDER TRUST FACTORS
# Adopted from the retired kisanshakti-ndvi-engine repo, which handled this
# better than my original design.
# ---------------------------------------------------------------------------
# 29% of this tenant's fields are under 0.5 acre; the smallest is 0.23 acre
# (~930 m2 ~ 9 Sentinel-2 pixels). At that size nearly every pixel is an edge
# pixel. The value is still usable, but it must not carry full weight.
MICRO_LAND_ACRES = 0.25
MICRO_LAND_FACTOR = 0.65

# A 40 m buffer around a centroid is a guess at where the field is. It should
# never score like a surveyed polygon.
GEOMETRY_CONFIDENCE_FACTOR = {"high": 1.0, "medium": 0.85, "low": 0.55}

# ---------------------------------------------------------------------------
# HISTOGRAM
# 20 bins over [-1, 1]. ~200 bytes, and it lets percentiles, uniformity and
# patch structure be recomputed later without touching imagery again.
# PER FIELD ONLY - never a tile aggregate (see migration 006 Part A).
# ---------------------------------------------------------------------------
NDVI_HISTOGRAM_BINS = 20
