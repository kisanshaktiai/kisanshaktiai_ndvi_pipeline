import os
import json
import numpy as np
import matplotlib.pyplot as plt
from matplotlib import colors
from rasterio.transform import array_bounds
from typing import Tuple, Optional
from supabase import Client

from logger import logger


# ---------------------------------------------------------
# NDVI COLOR MAP (AGRONOMY FRIENDLY)
# ---------------------------------------------------------
NDVI_COLORMAP = colors.LinearSegmentedColormap.from_list(
    "ndvi",
    [
        "#654321",  # bare soil / very low
        "#ffcc00",  # weak vegetation
        "#7ec850",  # good vegetation
        "#1a9850",  # excellent vegetation
    ],
)


def generate_ndvi_thumbnail(
    ndvi_array: np.ndarray,
    transform,
    land_id: str,
    supabase: Client,
    size: int = 256,
    vmin: float = -0.2,
    vmax: float = 0.9,
) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """
    Generate NDVI PNG thumbnail + geospatial metadata and upload to Supabase Storage.

    Args:
        ndvi_array: NDVI numpy array (2D)
        transform: rasterio affine transform
        land_id: UUID of land
        supabase: Supabase client for storage upload
        size: Thumbnail size in pixels
        vmin, vmax: NDVI value range for colormap

    Returns:
        Tuple of (public_url, local_png_path, local_json_path)
        public_url: Supabase Storage URL if upload successful, None otherwise
        local_png_path: Local PNG file path
        local_json_path: Local JSON metadata file path
    """

    # --------------------------------------------------
    # 1. Output directories
    # --------------------------------------------------
    base_dir = os.path.join("thumbnails", "ndvi")
    os.makedirs(base_dir, exist_ok=True)

    # --------------------------------------------------
    # 2. NDVI sanitization
    # --------------------------------------------------
    ndvi = np.squeeze(ndvi_array).astype("float32")

    ndvi = np.clip(ndvi, vmin, vmax)
    ndvi[np.isnan(ndvi)] = vmin

    # --------------------------------------------------
    # 3. Compute geographic bounds
    # --------------------------------------------------
    height, width = ndvi.shape

    west, south, east, north = array_bounds(
        height,
        width,
        transform
    )

    # --------------------------------------------------
    # 4. Render PNG thumbnail
    # --------------------------------------------------
    fig, ax = plt.subplots(
        figsize=(size / 64, size / 64),
        dpi=64
    )

    ax.imshow(
        ndvi,
        cmap=NDVI_COLORMAP,
        vmin=vmin,
        vmax=vmax
    )
    ax.axis("off")

    png_path = os.path.join(base_dir, f"{land_id}.png")
    fig.savefig(
        png_path,
        bbox_inches="tight",
        pad_inches=0,
        transparent=True
    )
    plt.close(fig)

    # --------------------------------------------------
    # 5. Save metadata JSON
    # --------------------------------------------------
    metadata = {
        "land_id": land_id,
        "crs": "EPSG:4326",
        "bounds": {
            "north": north,
            "south": south,
            "east": east,
            "west": west
        },
        "ndvi_range": {
            "min": vmin,
            "max": vmax
        },
        "resolution": {
            "width": width,
            "height": height
        }
    }

    json_path = os.path.join(base_dir, f"{land_id}.json")
    with open(json_path, "w") as f:
        json.dump(metadata, f, indent=2)

    # --------------------------------------------------
    # 6. Upload to Supabase Storage
    # --------------------------------------------------
    try:
        from storage import upload_ndvi_thumbnail
        
        public_url = upload_ndvi_thumbnail(
            supabase=supabase,
            land_id=land_id,
            png_path=png_path,
            json_path=json_path
        )
        
        if public_url:
            logger.info(f"NDVI thumbnail uploaded: {land_id}")
            return public_url, png_path, json_path
        else:
            logger.warning(f"NDVI thumbnail upload failed: {land_id}")
            # Return local path as fallback
            return f"/thumbnails/ndvi/{land_id}.png", png_path, json_path
            
    except Exception as e:
        logger.error(f"Error uploading NDVI thumbnail for {land_id}: {e}")
        # Return local path as fallback
        return f"/thumbnails/ndvi/{land_id}.png", png_path, json_path
