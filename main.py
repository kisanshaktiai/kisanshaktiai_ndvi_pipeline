from datetime import date, datetime, UTC
from typing import Dict
from db import fetch_lands


from db import (
    fetch_lands,
    insert_ndvi,
    update_land,
    log_step,
)
from processor import process_land
from analysis import crop_health
from logger import logger


def build_ndvi_row(
    land: Dict,
    result: Dict,
    health_label: str,
    alerts: list[str],
) -> Dict:

    return {
        "land_id": land["id"],
        "tenant_id": land["tenant_id"],

        # ✅ DATE (NOT datetime)
        "date": date.today().isoformat(),

        # Core NDVI
        "ndvi_value": round(result["ndvi_mean"], 3),
        "min_ndvi": round(result["ndvi_min"], 3),
        "max_ndvi": round(result["ndvi_max"], 3),

        # Water stress
        "ndwi_value": round(result["ndwi_mean"], 3),

        # Sentinel-1 soil moisture (nullable)
        "soil_moisture": result["soil_moisture"],

        # Image
        "image_url": result["ndvi_thumbnail_url"],

        # Metadata (analytics-only)
        "metadata": {
            "ndvi_trend": round(result["ndvi_trend"], 4),
            "ndre_trend": round(result["ndre_trend"], 4),
            "health_label": health_label,
            "alerts": alerts,
            "valid_observations": result["valid_observations"],
        },

        # Processing info
        "computed_at": datetime.now(UTC).isoformat(),
        "satellite_source": "sentinel-2",
        "collection_id": "sentinel-2-l2a",
        "processing_level": "L2A",
        "spatial_resolution": 10,
    }

def main():
    logger.info("NDVI pipeline started")

    lands = fetch_lands()

    if not lands:
        logger.info("No active lands found")
        return

    for land in lands:
        land_id = land["id"]
        tenant_id = land["tenant_id"]

        try:
            # --------------------------------------------------
            # LOG: START
            # --------------------------------------------------
            log_step(
                step="PROCESS_START",
                status="running",
                land_id=land_id,
                tenant_id=tenant_id,
            )

            # --------------------------------------------------
            # PROCESS LAND
            # --------------------------------------------------
            result = process_land(land)

            if result is None:
                log_step(
                    step="PROCESS_SKIPPED",
                    status="no_data",
                    land_id=land_id,
                    tenant_id=tenant_id,
                )
                continue

            # --------------------------------------------------
            # AGRONOMIC INTERPRETATION
            # --------------------------------------------------
            health_label, alerts = crop_health(
                ndvi_mean=result["ndvi_mean"],
                ndvi_trend=result["ndvi_trend"],
                ndre_trend=result.get("ndre_trend"),
                ndwi_mean=result.get("ndwi_mean"),
                soil_moisture=result.get("soil_moisture"),
            )


            # --------------------------------------------------
            # INSERT NDVI DATA
            # --------------------------------------------------
            ndvi_row = build_ndvi_row(
                land=land,
                result=result,
                health_label=health_label,
                alerts=alerts,
            )

            insert_ndvi(ndvi_row)

            # --------------------------------------------------
            # UPDATE LANDS TABLE
            # --------------------------------------------------
            update_land(
                land_id=land_id,
                ndvi=result["ndvi_mean"],
                thumbnail_url=result["ndvi_thumbnail_url"],
            )

            # --------------------------------------------------
            # LOG: SUCCESS
            # --------------------------------------------------
            log_step(
                step="PROCESS_END",
                status="success",
                land_id=land_id,
                tenant_id=tenant_id,
            )

        except Exception as e:
            logger.exception(f"Land processing failed: {land_id}")

            log_step(
                step="PROCESS_ERROR",
                status="error",
                land_id=land_id,
                tenant_id=tenant_id,
                error=str(e),
            )

    logger.info("NDVI pipeline finished")


if __name__ == "__main__":
    main()
