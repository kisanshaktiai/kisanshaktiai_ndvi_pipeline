import numpy as np

# ---------------------------------------------------------
# CROP-SPECIFIC NDVI THRESHOLDS (Agronomic Research-Based)
# ---------------------------------------------------------
CROP_NDVI_THRESHOLDS = {
    # Rice (paddy) - water background affects NDVI
    "rice": {
        "critical": 0.25,
        "low": 0.40,
        "moderate": 0.55,
        "healthy": 0.70,
    },
    # Wheat - moderate canopy density
    "wheat": {
        "critical": 0.35,
        "low": 0.50,
        "moderate": 0.65,
        "healthy": 0.75,
    },
    # Sugarcane - dense canopy, high NDVI
    "sugarcane": {
        "critical": 0.45,
        "low": 0.60,
        "moderate": 0.75,
        "healthy": 0.85,
    },
    # Cotton - moderate to high canopy
    "cotton": {
        "critical": 0.30,
        "low": 0.45,
        "moderate": 0.60,
        "healthy": 0.70,
    },
    # Maize/Corn - high biomass
    "maize": {
        "critical": 0.35,
        "low": 0.50,
        "moderate": 0.70,
        "healthy": 0.80,
    },
    "corn": {  # Alias for maize
        "critical": 0.35,
        "low": 0.50,
        "moderate": 0.70,
        "healthy": 0.80,
    },
    # Soybean - moderate canopy
    "soybean": {
        "critical": 0.30,
        "low": 0.45,
        "moderate": 0.65,
        "healthy": 0.75,
    },
    # Vegetables - variable, moderate default
    "vegetables": {
        "critical": 0.25,
        "low": 0.40,
        "moderate": 0.55,
        "healthy": 0.70,
    },
    # Default for unknown crops
    "default": {
        "critical": 0.30,
        "low": 0.45,
        "moderate": 0.60,
        "healthy": 0.70,
    },
}


# ---------------------------------------------------------
# NDVI / NDRE TREND CALCULATION
# ---------------------------------------------------------
def trend(values: list[float]) -> float:
    """
    Linear trend slope over time using least squares regression.
    
    Positive slope = vegetation increasing
    Negative slope = vegetation declining
    
    Args:
        values: Time-series of index values (NDVI, NDRE, etc.)
        
    Returns:
        Slope of linear trend (units per time step)
    """
    if len(values) < 2:
        return 0.0
    
    x = np.arange(len(values))
    
    try:
        # Linear regression: y = mx + b
        coefficients = np.polyfit(x, values, 1)
        slope = float(coefficients[0])
        
        return slope
    except:
        return 0.0


# ---------------------------------------------------------
# CROP HEALTH CLASSIFICATION (ENHANCED AGRONOMY)
# ---------------------------------------------------------
def crop_health(
    ndvi_mean: float,
    ndvi_trend: float,
    ndre_trend: float | None = None,
    ndwi_mean: float | None = None,
    soil_moisture: float | None = None,
    crop_type: str | None = None,
    ndvi_cv: float | None = None,
):
    """
    Determines crop health label and advisory alerts using:
    - Multi-spectral vegetation indices (NDVI, NDRE, NDWI)
    - Temporal trends
    - SAR soil moisture
    - Crop-specific thresholds
    
    Args:
        ndvi_mean: Mean NDVI value (-1 to 1, typically 0-1 for vegetation)
        ndvi_trend: Temporal trend slope (positive=growing, negative=declining)
        ndre_trend: Red-edge trend (nitrogen stress indicator)
        ndwi_mean: Normalized Difference Water Index (water stress)
        soil_moisture: Sentinel-1 SAR soil moisture proxy (dB)
        crop_type: Crop name for specific thresholds
        ndvi_cv: Coefficient of variation (spatial uniformity)
        
    Returns:
        tuple: (health_label, list_of_alerts)
            health_label: "Healthy" | "Moderate" | "Critical" | "Unknown"
            alerts: List of actionable agronomic advisories
    """

    alerts: list[str] = []

    # -------------------------------------------------
    # DATA VALIDATION
    # -------------------------------------------------
    if ndvi_mean is None or not np.isfinite(ndvi_mean):
        return "Unknown", ["Insufficient satellite data"]

    # -------------------------------------------------
    # SELECT CROP-SPECIFIC THRESHOLDS
    # -------------------------------------------------
    crop_key = crop_type.lower() if crop_type else "default"
    thresholds = CROP_NDVI_THRESHOLDS.get(crop_key, CROP_NDVI_THRESHOLDS["default"])

    # -------------------------------------------------
    # 1. NDVI ANALYSIS – Vegetation vigor
    # -------------------------------------------------
    if ndvi_mean < thresholds["critical"]:
        alerts.append("Critical: Very low vegetation cover detected")
    elif ndvi_mean < thresholds["low"]:
        alerts.append("Low vegetation density - possible stress or early growth")
    elif ndvi_mean < thresholds["moderate"]:
        alerts.append("Moderate vegetation - monitor development")
    # Healthy range: no alert

    # -------------------------------------------------
    # 2. NDVI TREND – Growth direction
    # -------------------------------------------------
    TREND_THRESHOLD_DECLINE = -0.01  # Declining >1% per observation
    TREND_THRESHOLD_RAPID_DECLINE = -0.02  # Rapid decline
    
    if ndvi_trend < TREND_THRESHOLD_RAPID_DECLINE:
        alerts.append("Alert: Rapid vegetation decline detected")
    elif ndvi_trend < TREND_THRESHOLD_DECLINE:
        alerts.append("Warning: Vegetation growth declining")
    # Positive/stable trend: no alert

    # -------------------------------------------------
    # 3. NDRE TREND – Nitrogen stress
    # -------------------------------------------------
    if ndre_trend is not None:
        NDRE_DECLINE_THRESHOLD = -0.01
        
        if ndre_trend < NDRE_DECLINE_THRESHOLD:
            alerts.append("Possible nitrogen deficiency detected")

    # -------------------------------------------------
    # 4. NDWI – Water stress
    # -------------------------------------------------
    if ndwi_mean is not None:
        # NDWI interpretation:
        # > 0.3: High water content (flooded/irrigated)
        # 0 to 0.3: Adequate moisture
        # -0.1 to 0: Moderate water stress
        # < -0.2: Severe water stress
        
        if ndwi_mean < -0.25:
            alerts.append("Severe crop water stress - immediate irrigation recommended")
        elif ndwi_mean < -0.15:
            alerts.append("Moderate water stress detected - consider irrigation")

    # -------------------------------------------------
    # 5. SENTINEL-1 SAR – Soil moisture
    # -------------------------------------------------
    if soil_moisture is not None:
        # SAR backscatter interpretation (dB scale):
        # Higher values = wet soil (more backscatter)
        # Lower values = dry soil (less backscatter)
        # Note: Absolute values vary by soil type, crop, roughness
        
        if soil_moisture < -17:
            alerts.append("Critical soil moisture deficit - irrigation urgently needed")
        elif soil_moisture < -13:
            alerts.append("Low soil moisture - plan irrigation")
        elif soil_moisture < -10:
            alerts.append("Soil moisture declining - monitor closely")

    # -------------------------------------------------
    # 6. SPATIAL VARIABILITY – Field uniformity
    # -------------------------------------------------
    if ndvi_cv is not None:
        # Coefficient of Variation (CV) = std / mean
        # High CV indicates non-uniform growth (possible issues)
        
        if ndvi_cv > 0.30:
            alerts.append("High field variability detected - check for pest/disease/nutrient issues")
        elif ndvi_cv > 0.20:
            alerts.append("Moderate field variability - uneven growth patterns")

    # -------------------------------------------------
    # FINAL HEALTH LABEL DETERMINATION
    # -------------------------------------------------
    # Priority-based classification
    
    critical_keywords = ["critical", "severe", "urgently", "rapid decline"]
    has_critical_alert = any(
        any(kw in alert.lower() for kw in critical_keywords)
        for alert in alerts
    )
    
    if has_critical_alert or ndvi_mean < thresholds["critical"]:
        label = "Critical"
    elif len(alerts) == 0:
        label = "Healthy"
    elif len(alerts) == 1 or ndvi_mean >= thresholds["moderate"]:
        label = "Moderate"
    else:
        label = "Critical"

    return label, alerts


# ---------------------------------------------------------
# ADVANCED ANALYTICS (Optional)
# ---------------------------------------------------------
def vegetation_condition_index(ndvi_current: float, ndvi_min: float, ndvi_max: float) -> float:
    """
    Vegetation Condition Index (VCI) - Normalized NDVI relative to historical range.
    
    VCI = 100 * (NDVI_current - NDVI_min) / (NDVI_max - NDVI_min)
    
    Interpretation:
        VCI < 35: Drought/stress conditions
        VCI 35-50: Below normal
        VCI 50-65: Normal
        VCI > 65: Above normal
    
    Args:
        ndvi_current: Current observation
        ndvi_min: Historical minimum (multi-year)
        ndvi_max: Historical maximum (multi-year)
        
    Returns:
        VCI value (0-100)
    """
    if ndvi_max <= ndvi_min:
        return 50.0  # Default to normal
    
    vci = 100 * (ndvi_current - ndvi_min) / (ndvi_max - ndvi_min)
    return float(np.clip(vci, 0, 100))


def growth_stage_estimation(ndvi_series: list[float], crop_type: str) -> str:
    """
    Estimate crop growth stage from NDVI time-series patterns.
    
    This is a simplified approach - real growth stage estimation requires
    phenology models and ground truth calibration.
    
    Args:
        ndvi_series: Time-ordered NDVI values
        crop_type: Crop name
        
    Returns:
        Estimated growth stage string
    """
    if len(ndvi_series) < 3:
        return "Unknown"
    
    # Compute trend
    ndvi_trend = trend(ndvi_series)
    current_ndvi = ndvi_series[-1]
    
    # Simplified stage estimation
    thresholds = CROP_NDVI_THRESHOLDS.get(crop_type.lower(), CROP_NDVI_THRESHOLDS["default"])
    
    if current_ndvi < thresholds["low"]:
        if ndvi_trend > 0.01:
            return "Early vegetative (emerging)"
        else:
            return "Senescence or stress"
    elif current_ndvi < thresholds["moderate"]:
        if ndvi_trend > 0.01:
            return "Vegetative growth"
        else:
            return "Mid-season or declining"
    else:
        if abs(ndvi_trend) < 0.01:
            return "Peak vegetation (flowering/grain fill)"
        elif ndvi_trend > 0:
            return "Late vegetative"
        else:
            return "Maturity/senescence"
