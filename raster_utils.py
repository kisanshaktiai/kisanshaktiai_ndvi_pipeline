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
    
    CRITICAL FIX: Ensures bands are in reflectance scale (0-1)
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
        # CRITICAL FIX: Check and convert band scale
        # ============================================================
        # Sentinel-2 L2A should be in reflectance (0-1)
        # But some sources provide DN scale (0-10000)
        # This causes MCARI to produce impossible values (millions/billions)
        
        max_val = np.nanmax(data)
        
        if max_val > 10.0:
            # Data is in DN scale (0-10000), convert to reflectance (0-1)
            data = data / 10000.0
            logger.debug(
                f"Converted band from DN scale to reflectance "
                f"(max: {max_val:.0f} → {np.nanmax(data):.4f})"
            )
        elif max_val > 1.0 and max_val <= 10.0:
            # Ambiguous range - log warning
            logger.warning(
                f"Band has unusual scale (max={max_val:.2f}). "
                f"Expected 0-1 (reflectance) or 0-10000 (DN). "
                f"Treating as reflectance but indices may be inaccurate."
            )
        
        # Additional validation: Check for reasonable reflectance range
        if np.nanmax(data) > 1.5:
            logger.error(
                f"Band reflectance exceeds 1.5 (max={np.nanmax(data):.2f}). "
                f"Data quality issue - indices will be unreliable."
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
