import os
import json
import numpy as np
import matplotlib.pyplot as plt
from matplotlib import colors
from rasterio.transform import array_bounds


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
    size: int = 256,
    vmin: float = -0.2,
    vmax: float = 0.9,
) -> str:
    """
    Generate NDVI PNG thumbnail + geospatial metadata.

    Outputs:
    - PNG thumbnail
    - JSON metadata (lat/lon bounds for map overlay)

    Returns:
    - Relative/public URL to PNG
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
    # 5. Save metadata JSON (CRITICAL)
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
    # 6. Return public path
    # --------------------------------------------------
    return f"/thumbnails/ndvi/{land_id}.png"
