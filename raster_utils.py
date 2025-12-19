import numpy as np
import rasterio
from rasterio.mask import mask
from rasterio.warp import reproject, Resampling
from shapely.ops import transform
from shapely.geometry import mapping
from pyproj import Transformer

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
