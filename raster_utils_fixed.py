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
    
    CRITICAL FIX: AGGRESSIVE band scale detection and conversion
    Handles DN scale (0-10000), ambiguous scale (1-10), and reflectance (0-1)
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
        # CRITICAL FIX: AGGRESSIVE scale detection and conversion
        # ============================================================
        max_val = np.nanmax(data)
        min_val = np.nanmin(data)
        
        # STRATEGY: Convert ANYTHING > 1.0 to reflectance scale
        # This handles DN (10000), ambiguous (5-10), and edge cases
        
        if max_val > 1.0:
            # Assume DN scale and convert
            data = data / 10000.0
            logger.debug(
                f"🔧 Converted band: DN scale detected "
                f"(original max={max_val:.1f} → reflectance max={np.nanmax(data):.4f})"
            )
            max_val = np.nanmax(data)  # Update for validation
        
        # ============================================================
        # VALIDATION: Final reflectance check
        # ============================================================
        if max_val > 1.2:
            # Still too high - this is a serious problem
            logger.error(
                f"❌ CRITICAL: Band reflectance still > 1.2 after conversion "
                f"(max={max_val:.4f}). Data quality issue!"
            )
            # Force clip to prevent index calculation failures
            data = np.clip(data, 0.0, 1.0)
            logger.warning(f"⚠️  Force-clipped band to 0-1 range")
        
        if max_val < 0.0:
            logger.error(f"❌ Band has negative values (max={max_val:.4f})")
            data = np.clip(data, 0.0, 1.0)
        
        # Log final validation
        logger.debug(
            f"✅ Band validated: range=[{np.nanmin(data):.4f}, {np.nanmax(data):.4f}], "
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
