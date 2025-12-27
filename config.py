# ---------------------------------------------------------
# GLOBAL PIPELINE CONFIGURATION
# ---------------------------------------------------------

# Lookback window for satellite data (days)
LOOKBACK_DAYS = 15

# Sentinel-1 lookback window (longer for SAR availability)
S1_LOOKBACK_DAYS = 30  # Increased from 15 to improve data availability

# Sentinel collections (Planetary Computer)
S2_COLLECTION = "sentinel-2-l2a"
S1_COLLECTION = "sentinel-1-grd"

# Sentinel-2 Scene Classification Layer (SCL)
# 4 = Vegetation, 5 = Bare soil, 6 = Water
VALID_SCL = [4, 5, 6]

# Minimum number of valid NDVI observations
# Needed for trend + stability
MIN_VALID_PIXELS = 4

# Cloud cover threshold (percent)
MAX_CLOUD_PERCENT = 30
