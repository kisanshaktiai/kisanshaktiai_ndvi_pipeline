import numpy as np

# ---------------------------------------------------------
# NDVI / NDRE / MCARI TREND CALCULATION
# ---------------------------------------------------------
def trend(values: list[float]) -> float:
    """
    Linear trend slope over time
    """
    if len(values) < 2:
        return 0.0
    x = np.arange(len(values))
    return float(np.polyfit(x, values, 1)[0])


# ---------------------------------------------------------
# CROP HEALTH CLASSIFICATION (AGRONOMY GRADE)
# ---------------------------------------------------------
def crop_health(
    ndvi_mean: float,
    ndvi_trend: float,
    ndre_trend: float | None = None,
    ndwi_mean: float | None = None,
    soil_moisture: float | None = None,
    mcari_mean: float | None = None,  # NEW: MCARI input
    mcari_trend: float | None = None,  # NEW: MCARI trend
):
    """
    Determines crop health label and advisory alerts
    using satellite vegetation, water and soil indicators.
    
    Enhanced with MCARI for better chlorophyll/nitrogen assessment.
    """

    alerts: list[str] = []

    # -------------------------------------------------
    # NDVI – Vegetation vigor
    # -------------------------------------------------
    if ndvi_mean is None:
        return "Unknown", ["Insufficient satellite data"]

    if ndvi_mean < 0.30:
        alerts.append("Very low vegetation cover")
    elif ndvi_mean < 0.45:
        alerts.append("Moderate vegetation stress")

    # -------------------------------------------------
    # NDVI Trend – Growth direction
    # -------------------------------------------------
    if ndvi_trend < -0.01:
        alerts.append("Vegetative growth declining")

    # -------------------------------------------------
    # MCARI – Chlorophyll content & Nitrogen stress
    # NEW: More sensitive than NDRE for early detection
    # -------------------------------------------------
    if mcari_mean is not None:
        # MCARI typical range: 0 to 2+ (higher = more chlorophyll)
        if mcari_mean < 0.5:
            alerts.append("Low chlorophyll content detected")
        elif mcari_mean < 0.8:
            alerts.append("Moderate chlorophyll stress")
    
    if mcari_trend is not None and mcari_trend < -0.05:
        alerts.append("Chlorophyll declining rapidly (possible N deficiency)")

    # -------------------------------------------------
    # NDRE – Nitrogen stress (complementary to MCARI)
    # -------------------------------------------------
    if ndre_trend is not None and ndre_trend < -0.01:
        alerts.append("Possible nitrogen deficiency")

    # -------------------------------------------------
    # NDWI – Water stress
    # -------------------------------------------------
    if ndwi_mean is not None and ndwi_mean < -0.20:
        alerts.append("Crop water stress likely")

    # -------------------------------------------------
    # Sentinel-1 SAR – Soil moisture
    # -------------------------------------------------
    if soil_moisture is not None:
        if soil_moisture < -17:
            alerts.append("Severe soil moisture deficit")
        elif soil_moisture < -12:
            alerts.append("Low soil moisture")

    # -------------------------------------------------
    # FINAL HEALTH LABEL (enhanced with MCARI)
    # -------------------------------------------------
    # Prioritize chlorophyll/nitrogen issues detected by MCARI
    critical_keywords = ["rapidly", "severe", "very low", "declining"]
    has_critical = any(
        any(keyword in alert.lower() for keyword in critical_keywords)
        for alert in alerts
    )
    
    if not alerts:
        label = "Healthy"
    elif has_critical or len(alerts) >= 3:
        label = "Critical"
    elif len(alerts) == 2:
        label = "Moderate"
    else:
        label = "Moderate"

    return label, alerts
