import numpy as np

# ---------------------------------------------------------
# NDVI / NDRE TREND CALCULATION
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
):
    """
    Determines crop health label and advisory alerts
    using satellite vegetation, water and soil indicators.
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
    # NDRE – Nitrogen stress
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
    # FINAL HEALTH LABEL
    # -------------------------------------------------
    if not alerts:
        label = "Healthy"
    elif len(alerts) == 1:
        label = "Moderate"
    else:
        label = "Critical"

    return label, alerts
