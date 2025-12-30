import numpy as np
import rasterio
from rasterio.mask import mask
from rasterio.warp import reproject, Resampling
from shapely.ops import transform
from shapely.geometry import mapping
from pyproj import Transformer

from logger import logger

# Sentinel-2 Scene Classification Layer (SCL)
# Valid vegetation pixels
VALID_SCL = [4, 5, 6, 7]  # Vegetation, Bare soil, Water (optional)


def reproject_geometry(geom, dst_crs):
    transformer = Transformer.from_crs(
        "EPSG:4326",
        dst_crs,
        always_xy=True
    )
    return transform(transformer.transform, geom)


def read_band(asset, geometry, reference=None):
    """
    Always returns (array, transform)
    array dtype: float32
    
    CRITICAL FIX: ENFORCES reflectance scale (0-1) with strict validation
    """
    with rasterio.open(asset.href) as src:
        geom_proj = reproject_geometry(geometry, src.crs)

        data, transform = mask(
            src,
            [mapping(geom_proj)],
            crop=True,
            filled=True
        )

        data = data.astype("float32")
        
        # ============================================================
        # CRITICAL FIX: Mandatory band scale detection and conversion
        # ============================================================
        max_val = np.nanmax(data)
        min_val = np.nanmin(data)
        
        # Case 1: DN scale (0-10000) - MUST convert
        if max_val > 10.0:
            logger.info(
                f"🔧 Converting band from DN scale to reflectance "
                f"(range: {min_val:.0f}-{max_val:.0f} → "
                f"{min_val/10000:.4f}-{max_val/10000:.4f})"
            )
            data = data / 10000.0
            max_val = np.nanmax(data)
        
        # Case 2: Ambiguous scale (1-10) - FORCE conversion to be safe
        elif max_val > 1.0:
            logger.warning(
                f"⚠️  Ambiguous band scale detected (max={max_val:.2f}). "
                f"Assuming DN scale and converting to reflectance."
            )
            data = data / 10000.0
            max_val = np.nanmax(data)
        
        # Case 3: Already in reflectance (0-1)
        else:
            logger.debug(
                f"✅ Band already in reflectance scale (max={max_val:.4f})"
            )
        
        # ============================================================
        # VALIDATION: Check final values are physically reasonable
        # ============================================================
        if max_val > 1.5:
            logger.error(
                f"❌ Band reflectance exceeds 1.5 (max={max_val:.2f}). "
                f"Data quality issue - indices will be UNRELIABLE."
            )
            raise ValueError(
                f"Invalid band reflectance: max={max_val:.2f}. "
                f"Expected 0-1 range for vegetation indices."
            )
        
        if max_val < 0.0:
            logger.error(
                f"❌ Band reflectance is negative (max={max_val:.2f}). "
                f"Data corruption detected."
            )
            raise ValueError(
                f"Invalid band reflectance: max={max_val:.2f}. "
                f"Cannot be negative."
            )
        
        # Log successful validation
        logger.debug(
            f"✅ Band validated: min={np.nanmin(data):.4f}, "
            f"max={np.nanmax(data):.4f}, "
            f"mean={np.nanmean(data):.4f}"
        )
        
        # ============================================================
        
        if reference is not None:
            ref_data, ref_transform = reference
            dst = np.empty_like(ref_data, dtype="float32")

            reproject(
                source=data,
                destination=dst,
                src_transform=transform,
                src_crs=src.crs,
                dst_transform=ref_transform,
                dst_crs=src.crs,
                resampling=Resampling.bilinear,
            )
            return dst, ref_transform

        return data, transform


def cloud_mask(bands: dict):
    """
    Apply cloud masking using Sentinel-2 SCL band.
    """
    scl = bands.get("SCL")
    if scl is None:
        return bands

    valid_mask = np.isin(scl, VALID_SCL)

    for k in bands:
        if k != "SCL":
            bands[k] = np.where(valid_mask, bands[k], np.nan)

    return bands
