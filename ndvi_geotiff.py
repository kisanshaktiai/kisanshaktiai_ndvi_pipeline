import os
import numpy as np
import rasterio
from rasterio.enums import Compression
from rasterio.transform import Affine
from typing import Optional, Tuple
from supabase import Client

from logger import logger


def write_ndvi_geotiff(
    ndvi_array: np.ndarray,
    transform: Affine,
    land_id: str,
    supabase: Client = None,
    crs: str = "EPSG:4326",
    vmin: float = -0.2,
    vmax: float = 0.9,
) -> Tuple[Optional[str], Optional[str]]:
    """
    Write NDVI GeoTIFF with full georeferencing and upload to Supabase Storage.

    Args:
        ndvi_array: NDVI numpy array (2D)
        transform: rasterio affine transform
        land_id: UUID of land
        supabase: Supabase client for storage upload (optional)
        crs: Coordinate Reference System
        vmin, vmax: NDVI clipping range

    Returns:
        Tuple of (public_url, local_path)
        public_url: Supabase Storage URL if upload successful, None otherwise
        local_path: Local GeoTIFF file path
    """

    # --------------------------------------------------
    # 1. Output directory
    # --------------------------------------------------
    out_dir = os.path.join("rasters", "ndvi")
    os.makedirs(out_dir, exist_ok=True)

    output_path = os.path.join(out_dir, f"{land_id}_ndvi.tif")

    # --------------------------------------------------
    # 2. NDVI sanitization
    # --------------------------------------------------
    ndvi = np.squeeze(ndvi_array).astype("float32")

    ndvi = np.clip(ndvi, vmin, vmax)
    ndvi[np.isnan(ndvi)] = -9999.0  # nodata value

    height, width = ndvi.shape

    # --------------------------------------------------
    # 3. Write GeoTIFF
    # --------------------------------------------------
    with rasterio.open(
        output_path,
        "w",
        driver="GTiff",
        height=height,
        width=width,
        count=1,
        dtype="float32",
        crs=crs,
        transform=transform,
        nodata=-9999.0,
        compress=Compression.deflate,
        tiled=True,
        blockxsize=256,
        blockysize=256,
    ) as dst:
        dst.write(ndvi, 1)

    logger.info(f"NDVI GeoTIFF created locally: {output_path}")

    # --------------------------------------------------
    # 4. Upload to Supabase Storage (if client provided)
    # --------------------------------------------------
    if supabase is None:
        logger.warning("No Supabase client provided, skipping upload")
        return None, output_path

    try:
        from storage import upload_ndvi_geotiff
        
        public_url = upload_ndvi_geotiff(
            supabase=supabase,
            land_id=land_id,
            geotiff_path=output_path
        )
        
        if public_url:
            logger.info(f"NDVI GeoTIFF uploaded: {land_id}")
            return public_url, output_path
        else:
            logger.warning(f"NDVI GeoTIFF upload failed: {land_id}")
            return None, output_path
            
    except Exception as e:
        logger.error(f"Error uploading NDVI GeoTIFF for {land_id}: {e}")
        return None, output_path
