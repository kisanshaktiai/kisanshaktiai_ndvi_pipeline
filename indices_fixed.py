import numpy as np
from logger import logger


def compute_indices(b):
    """
    Compute vegetation indices from Sentinel-2 bands.
    
    CRITICAL: Assumes bands are in reflectance scale (0-1).
    All bands MUST be validated by read_band() before calling this function.
    
    Enhanced with strict MCARI validation to prevent impossible values.
    """
    # Validate inputs
    for k, v in b.items():
        if not isinstance(v, np.ndarray):
            raise TypeError(f"Band {k} is not ndarray: {type(v)}")
    
    # ============================================================
    # PRE-CALCULATION VALIDATION: Check band ranges
    # ============================================================
    band_ranges = {}
    for band_name in ["B03", "B04", "B05", "B08"]:
        if band_name in b:
            band_min = np.nanmin(b[band_name])
            band_max = np.nanmax(b[band_name])
            band_mean = np.nanmean(b[band_name])
            band_ranges[band_name] = {
                "min": band_min,
                "max": band_max,
                "mean": band_mean
            }
            
            # CRITICAL CHECK: Bands must be 0-1 range
            if band_max > 1.5:
                logger.error(
                    f"❌ CRITICAL: Band {band_name} exceeds reflectance range! "
                    f"max={band_max:.2f}. MCARI will be wrong!"
                )
                raise ValueError(
                    f"Band {band_name} not in reflectance scale (0-1). "
                    f"Found max={band_max:.2f}. Check read_band() conversion."
                )
            
            if band_min < -0.1:
                logger.warning(
                    f"⚠️  Band {band_name} has unexpected negative values: "
                    f"min={band_min:.4f}"
                )
    
    logger.debug(
        f"Band ranges validated: "
        f"B03=[{band_ranges['B03']['min']:.3f}, {band_ranges['B03']['max']:.3f}], "
        f"B04=[{band_ranges['B04']['min']:.3f}, {band_ranges['B04']['max']:.3f}], "
        f"B05=[{band_ranges['B05']['min']:.3f}, {band_ranges['B05']['max']:.3f}], "
        f"B08=[{band_ranges['B08']['min']:.3f}, {band_ranges['B08']['max']:.3f}]"
    )
    
    # ============================================================
    # NDVI - Normalized Difference Vegetation Index
    # ============================================================
    ndvi = (b["B08"] - b["B04"]) / (b["B08"] + b["B04"] + 1e-6)
    
    # ============================================================
    # NDRE - Normalized Difference Red Edge (Nitrogen stress)
    # ============================================================
    ndre = (b["B08"] - b["B05"]) / (b["B08"] + b["B05"] + 1e-6)
    
    # ============================================================
    # NDWI - Normalized Difference Water Index
    # ============================================================
    ndwi = (b["B03"] - b["B08"]) / (b["B03"] + b["B08"] + 1e-6)
    
    # ============================================================
    # MCARI - Modified Chlorophyll Absorption Ratio Index
    # Formula: [(B05 - B04) - 0.2 × (B05 - B03)] × (B05 / B04)
    # ============================================================
    
    # Step 1: Calculate band differences
    red_edge_red_diff = b["B05"] - b["B04"]  # Red edge - Red
    red_edge_green_diff = b["B05"] - b["B03"]  # Red edge - Green
    
    # Step 2: Safe division - avoid divide by zero or very small values
    # If B04 (Red) is very small (<0.01 reflectance), MCARI is unreliable
    b04_safe = np.where(b["B04"] > 0.01, b["B04"], np.nan)
    
    # Step 3: Calculate MCARI with safety checks
    mcari = (red_edge_red_diff - 0.2 * red_edge_green_diff) * (b["B05"] / b04_safe)
    
    # ============================================================
    # MCARI VALIDATION: Strict quality control
    # ============================================================
    
    # Get raw MCARI stats BEFORE clipping
    mcari_raw_min = np.nanmin(mcari)
    mcari_raw_max = np.nanmax(mcari)
    mcari_raw_mean = np.nanmean(mcari)
    
    # Check for impossible values (indicates scale issues)
    if abs(mcari_raw_max) > 100:
        logger.error(
            f"❌ MCARI CALCULATION FAILED! "
            f"Raw values out of range: min={mcari_raw_min:.1f}, "
            f"max={mcari_raw_max:.1f}, mean={mcari_raw_mean:.1f}. "
            f"Expected: -1 to +5. THIS INDICATES BAND SCALE ERROR!"
        )
        logger.error(
            f"Band statistics at failure: "
            f"B03={band_ranges['B03']}, "
            f"B04={band_ranges['B04']}, "
            f"B05={band_ranges['B05']}"
        )
        # Set MCARI to NaN for this scene
        mcari[:] = np.nan
    
    # Step 4: Clip to physically reasonable range
    # MCARI theoretical range: -1 to +5 (typical crop range: 0 to 2)
    mcari = np.clip(mcari, -1.0, 5.0)
    
    # Step 5: Set invalid values to NaN (same mask as NDVI)
    valid_mask = np.isfinite(ndvi) & np.isfinite(mcari)
    mcari = np.where(valid_mask, mcari, np.nan)
    
    # ============================================================
    # POST-CALCULATION DIAGNOSTICS
    # ============================================================
    mcari_final_mean = np.nanmean(mcari)
    mcari_final_min = np.nanmin(mcari)
    mcari_final_max = np.nanmax(mcari)
    
    if np.isfinite(mcari_final_mean):
        logger.debug(
            f"✅ MCARI calculated successfully: "
            f"mean={mcari_final_mean:.3f}, "
            f"range=[{mcari_final_min:.3f}, {mcari_final_max:.3f}]"
        )
    else:
        logger.warning(
            f"⚠️  MCARI all NaN (no valid pixels after masking)"
        )

    return {
        "NDVI": ndvi,
        "NDRE": ndre,
        "NDWI": ndwi,
        "MCARI": mcari,
    }
