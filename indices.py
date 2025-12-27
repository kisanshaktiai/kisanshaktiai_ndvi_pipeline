import numpy as np

def compute_indices(b):
    """
    Compute vegetation indices from Sentinel-2 bands.
    
    CRITICAL: Assumes bands are in reflectance scale (0-1).
    If bands are in DN scale (0-10000), they must be converted first.
    """
    for k, v in b.items():
        if not isinstance(v, np.ndarray):
            raise TypeError(f"Band {k} is not ndarray: {type(v)}")

    # NDVI - Normalized Difference Vegetation Index
    ndvi = (b["B08"] - b["B04"]) / (b["B08"] + b["B04"] + 1e-6)
    
    # NDRE - Normalized Difference Red Edge (Nitrogen stress)
    ndre = (b["B08"] - b["B05"]) / (b["B08"] + b["B05"] + 1e-6)
    
    # NDWI - Normalized Difference Water Index
    ndwi = (b["B03"] - b["B08"]) / (b["B03"] + b["B08"] + 1e-6)
    
    # MCARI - Modified Chlorophyll Absorption Ratio Index
    # Formula: [(B05 - B04) - 0.2 × (B05 - B03)] × (B05 / B04)
    # FIXED: Added proper validation and clipping
    
    # Step 1: Calculate band differences
    red_edge_red_diff = b["B05"] - b["B04"]  # Red edge - Red
    red_edge_green_diff = b["B05"] - b["B03"]  # Red edge - Green
    
    # Step 2: Safe division - avoid divide by zero or very small values
    # If B04 (Red) is very small (<0.01 reflectance), MCARI is unreliable
    b04_safe = np.where(b["B04"] > 0.01, b["B04"], np.nan)
    
    # Step 3: Calculate MCARI with safety checks
    mcari = (red_edge_red_diff - 0.2 * red_edge_green_diff) * (b["B05"] / b04_safe)
    
    # Step 4: Clip to physically reasonable range
    # MCARI theoretical range: -1 to +5 (typical crop range: 0 to 2)
    # Values outside this indicate data quality issues
    mcari = np.clip(mcari, -1.0, 5.0)
    
    # Step 5: Set invalid values to NaN (same mask as NDVI)
    # This ensures MCARI has same validity as other indices
    valid_mask = np.isfinite(ndvi) & np.isfinite(mcari)
    mcari = np.where(valid_mask, mcari, np.nan)

    return {
        "NDVI": ndvi,
        "NDRE": ndre,
        "NDWI": ndwi,
        "MCARI": mcari,
    }
