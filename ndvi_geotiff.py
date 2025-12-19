import os
import numpy as np
import rasterio
from rasterio.enums import Compression
from rasterio.transform import Affine


def write_ndvi_geotiff(
    ndvi_array: np.ndarray,
    transform: Affine,
    land_id: str,
    crs: str = "EPSG:4326",
    vmin: float = -0.2,
    vmax: float = 0.9,
) -> str:
    """
    Write NDVI GeoTIFF with full georeferencing.

    Args:
        ndvi_array: NDVI numpy array (2D)
        transform: rasterio affine transform
        land_id: UUID of land
        crs: Coordinate Reference System
        vmin, vmax: NDVI clipping range

    Returns:
        File path to NDVI GeoTIFF
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

    return output_path

