import numpy as np
from logger import logger


def compute_indices(b):
    """
    Compute vegetation indices from Sentinel-2 bands.
    
    CRITICAL: Assumes bands are in reflectance scale (0-1).
    FIXED: Robust MCARI calculation with comprehensive error handling.
    """
    # Validate inputs
    for k, v in b.items():
        if not isinstance(v, np.ndarray):
            raise TypeError(f"Band {k} is not ndarray: {type(v)}")
    
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
    # FIXED: Comprehensive error handling
    # ============================================================
    
    try:
        # Step 1: Calculate band differences
        red_edge_red_diff = b["B05"] - b["B04"]
        red_edge_green_diff = b["B05"] - b["B03"]
        
        # Step 2: Safe division - avoid divide by zero
        # Replace very small B04 values with NaN
        b04_safe = np.where(b["B04"] > 0.001, b["B04"], np.nan)
        
        # Step 3: Calculate MCARI
        mcari = (red_edge_red_diff - 0.2 * red_edge_green_diff) * (b["B05"] / b04_safe)
        
        # ============================================================
        # VALIDATION: Check for data quality issues
        # ============================================================
        
        # Check if we have any valid values
        valid_mcari = mcari[np.isfinite(mcari)]
        
        if len(valid_mcari) == 0:
            logger.warning(
                "⚠️  MCARI: No valid pixels after calculation. "
                "Setting entire array to NaN."
            )
            mcari = np.full_like(mcari, np.nan)
        else:
            # Check for extreme outliers
            mcari_max = np.nanmax(np.abs(mcari))
            
            if mcari_max > 100:
                # Extreme values indicate scale error
                logger.error(
                    f"❌ MCARI: Extreme values detected (max={mcari_max:.1f}). "
                    f"This indicates band scale issues. Setting to NaN."
                )
                mcari = np.full_like(mcari, np.nan)
            else:
                # Clip to reasonable range
                mcari = np.clip(mcari, -1.0, 5.0)
                
                # Apply same validity mask as NDVI
                valid_mask = np.isfinite(ndvi) & np.isfinite(mcari)
                mcari = np.where(valid_mask, mcari, np.nan)
                
                # Log success
                mcari_valid_after = mcari[np.isfinite(mcari)]
                if len(mcari_valid_after) > 0:
                    logger.debug(
                        f"✅ MCARI: mean={np.nanmean(mcari_valid_after):.3f}, "
                        f"range=[{np.nanmin(mcari_valid_after):.3f}, "
                        f"{np.nanmax(mcari_valid_after):.3f}], "
                        f"valid_pixels={len(mcari_valid_after)}"
                    )
                else:
                    logger.warning("⚠️  MCARI: All pixels masked after validation")
    
    except Exception as e:
        logger.error(f"❌ MCARI calculation failed: {e}")
        logger.exception("MCARI exception details:")
        # Set entire array to NaN on failure
        mcari = np.full_like(b["B04"], np.nan, dtype=np.float32)

    return {
        "NDVI": ndvi,
        "NDRE": ndre,
        "NDWI": ndwi,
        "MCARI": mcari,
    }
