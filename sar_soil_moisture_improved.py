"""
sar_soil_moisture.py
-------------------

Sentinel-1 SAR-based soil moisture estimation.

IMPORTANT: These are simplified proxy indices. For production-grade soil moisture:
- Use calibrated models (e.g., Change Detection, Water Cloud Model)
- Incorporate local soil properties (texture, organic matter)
- Validate against ground truth measurements
- Consider vegetation effects (crop type, biomass)

References:
- Bauer-Marschallinger et al. (2018): Copernicus Global Land Service
- El Hajj et al. (2017): Soil moisture retrieval from Sentinel-1
"""

import numpy as np


def to_db(arr: np.ndarray) -> np.ndarray:
    """
    Convert backscatter from linear to decibel (dB) scale.
    
    dB = 10 * log10(linear)
    
    Args:
        arr: Backscatter values in linear scale
        
    Returns:
        Backscatter in dB scale
    """
    # Avoid log(0) by adding small epsilon
    arr_safe = np.where(arr > 0, arr, 1e-10)
    return 10 * np.log10(arr_safe)


def soil_moisture_cross_ratio(vv: np.ndarray, vh: np.ndarray) -> float:
    """
    Soil moisture proxy using VH/VV cross-polarization ratio.
    
    Method: Cross-ratio in dB (VH_dB - VV_dB)
    
    Physical basis:
    - VV is more sensitive to soil dielectric constant (moisture)
    - VH is more sensitive to volume scattering (vegetation)
    - The ratio helps normalize vegetation effects
    
    Interpretation:
        Higher values (less negative) → Higher soil moisture
        Lower values (more negative) → Lower soil moisture
        
    Typical range: -15 to -8 dB
    
    Args:
        vv: VV polarization backscatter (linear)
        vh: VH polarization backscatter (linear)
        
    Returns:
        Mean cross-ratio in dB
    """
    vv_db = to_db(vv)
    vh_db = to_db(vh)
    
    # Cross-ratio: difference in dB = ratio in linear
    cross_ratio = vh_db - vv_db
    
    return float(np.nanmean(cross_ratio))


def soil_moisture_vv_index(vv: np.ndarray) -> float:
    """
    Simplified soil moisture proxy using VV backscatter only.
    
    Method: Direct VV_dB (simpler but less robust than cross-ratio)
    
    Physical basis:
    - VV backscatter increases with soil moisture
    - Works best for bare soil or sparse vegetation
    - Strongly affected by surface roughness and vegetation
    
    Interpretation:
        Higher values (less negative) → Wetter soil
        Lower values (more negative) → Drier soil
        
    Typical range: -20 to -5 dB
    
    Args:
        vv: VV polarization backscatter (linear)
        
    Returns:
        Mean VV backscatter in dB
    """
    vv_db = to_db(vv)
    return float(np.nanmean(vv_db))


def soil_moisture_combined(vv: np.ndarray, vh: np.ndarray) -> float:
    """
    Enhanced soil moisture index combining VV and VH.
    
    Method: Weighted combination of VV and cross-ratio
    
    Formula: 0.7 * VV_dB + 0.3 * (VH_dB - VV_dB)
    
    Rationale:
    - VV is primary soil moisture indicator
    - Cross-ratio helps reduce vegetation effects
    - Weights are empirical (can be calibrated locally)
    
    Args:
        vv: VV polarization backscatter (linear)
        vh: VH polarization backscatter (linear)
        
    Returns:
        Combined soil moisture index in dB
    """
    vv_db = to_db(vv)
    vh_db = to_db(vh)
    
    cross_ratio = vh_db - vv_db
    
    # Weighted combination
    combined_index = 0.7 * vv_db + 0.3 * cross_ratio
    
    return float(np.nanmean(combined_index))


def soil_moisture_change_detection(
    vv_current: np.ndarray,
    vv_reference: np.ndarray,
    vh_current: np.ndarray,
    vh_reference: np.ndarray,
) -> dict:
    """
    Change detection approach for relative soil moisture.
    
    Method: Compare current scene to dry reference condition
    
    This approach removes most of terrain and vegetation effects by
    looking at temporal changes rather than absolute values.
    
    Args:
        vv_current: Current VV backscatter
        vv_reference: Reference VV (dry condition)
        vh_current: Current VH backscatter
        vh_reference: Reference VH (dry condition)
        
    Returns:
        dict with change metrics
    """
    vv_current_db = to_db(vv_current)
    vv_reference_db = to_db(vv_reference)
    vh_current_db = to_db(vh_current)
    vh_reference_db = to_db(vh_reference)
    
    # Change in dB
    delta_vv = vv_current_db - vv_reference_db
    delta_vh = vh_current_db - vh_reference_db
    
    # Positive change = increased backscatter = wetter
    # Negative change = decreased backscatter = drier
    
    return {
        "delta_vv_db": float(np.nanmean(delta_vv)),
        "delta_vh_db": float(np.nanmean(delta_vh)),
        "moisture_change": "wetter" if np.nanmean(delta_vv) > 0 else "drier",
    }


# ---------------------------------------------------------
# MAIN FUNCTION (Recommended for general use)
# ---------------------------------------------------------
def soil_moisture(vv: np.ndarray, vh: np.ndarray, method: str = "combined") -> float:
    """
    Compute soil moisture proxy from Sentinel-1 SAR data.
    
    Args:
        vv: VV polarization backscatter (linear units)
        vh: VH polarization backscatter (linear units)
        method: Calculation method
            "combined" (default): Weighted VV + cross-ratio
            "cross_ratio": VH/VV ratio in dB
            "vv_only": Simple VV backscatter
            
    Returns:
        Soil moisture proxy in dB
        
    Interpretation guide:
        method="combined" or "vv_only":
            > -8 dB: Very wet (saturated, flooded)
            -8 to -12 dB: Wet to moist
            -12 to -15 dB: Moderate moisture
            -15 to -18 dB: Dry
            < -18 dB: Very dry
            
        method="cross_ratio":
            > -8 dB: High moisture
            -8 to -11 dB: Moderate-high moisture
            -11 to -14 dB: Moderate moisture
            < -14 dB: Low moisture
    
    Note: Absolute values are site-specific. Temporal trends are more reliable.
    """
    if method == "cross_ratio":
        return soil_moisture_cross_ratio(vv, vh)
    elif method == "vv_only":
        return soil_moisture_vv_index(vv)
    elif method == "combined":
        return soil_moisture_combined(vv, vh)
    else:
        raise ValueError(f"Unknown method: {method}. Use 'combined', 'cross_ratio', or 'vv_only'")


# ---------------------------------------------------------
# SPATIAL STATISTICS (Optional)
# ---------------------------------------------------------
def soil_moisture_stats(vv: np.ndarray, vh: np.ndarray, method: str = "combined") -> dict:
    """
    Compute spatial statistics of soil moisture across the field.
    
    Useful for identifying:
    - Wet/dry zones
    - Irrigation uniformity
    - Drainage issues
    
    Returns:
        dict with mean, std, percentiles
    """
    vv_db = to_db(vv)
    vh_db = to_db(vh)
    
    if method == "cross_ratio":
        index = vh_db - vv_db
    elif method == "vv_only":
        index = vv_db
    else:  # combined
        index = 0.7 * vv_db + 0.3 * (vh_db - vv_db)
    
    valid_index = index[np.isfinite(index)]
    
    if len(valid_index) == 0:
        return {
            "mean": None,
            "std": None,
            "min": None,
            "max": None,
            "p10": None,
            "p90": None,
        }
    
    return {
        "mean": float(np.mean(valid_index)),
        "std": float(np.std(valid_index)),
        "min": float(np.min(valid_index)),
        "max": float(np.max(valid_index)),
        "p10": float(np.percentile(valid_index, 10)),  # Driest 10%
        "p90": float(np.percentile(valid_index, 90)),  # Wettest 10%
        "cv": float(np.std(valid_index) / np.mean(valid_index)) if np.mean(valid_index) != 0 else 0,
    }
