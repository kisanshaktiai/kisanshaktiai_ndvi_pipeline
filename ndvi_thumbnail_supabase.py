import os
import json
import io
import numpy as np
import matplotlib.pyplot as plt
from matplotlib import colors
from rasterio.transform import array_bounds
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
    tenant_id: str,
    supabase: Client,
    size: int = 512,  # Increased for better quality
    vmin: float = -0.1,  # Adjusted range
    vmax: float = 1.0,
    bucket_name: str = "ndvi-thumbnails",
) -> tuple[str, dict]:
    """
    Generate NDVI PNG thumbnail + upload to Supabase Storage.

    Args:
        ndvi_array: NDVI numpy array (2D)
        transform: Rasterio affine transform
        land_id: UUID of land
        tenant_id: Tenant/organization ID
        supabase: Supabase client instance
        size: Thumbnail size in pixels
        vmin, vmax: NDVI value range for colormap
        bucket_name: Supabase Storage bucket name

    Returns:
        tuple: (public_url, metadata_dict)
    """

    # --------------------------------------------------
    # 1. NDVI sanitization & preprocessing
    # --------------------------------------------------
    ndvi = np.squeeze(ndvi_array).astype("float32")

    # Adaptive range for better contrast
    valid_ndvi = ndvi[np.isfinite(ndvi)]
    if len(valid_ndvi) > 0:
        # Use percentiles for adaptive stretching
        actual_min = float(np.percentile(valid_ndvi, 2))
        actual_max = float(np.percentile(valid_ndvi, 98))
        
        # Clamp to reasonable NDVI bounds
        display_min = max(vmin, actual_min)
        display_max = min(vmax, actual_max)
    else:
        display_min = vmin
        display_max = vmax

    # Clip and handle NaN
    ndvi_display = np.clip(ndvi, display_min, display_max)
    ndvi_display[np.isnan(ndvi_display)] = display_min

    # --------------------------------------------------
    # 2. Compute geographic bounds
    # --------------------------------------------------
    height, width = ndvi.shape

    west, south, east, north = array_bounds(
        height,
        width,
        transform
    )

    # --------------------------------------------------
    # 3. Render PNG thumbnail to in-memory buffer
    # --------------------------------------------------
    fig, ax = plt.subplots(
        figsize=(size / 100, size / 100),
        dpi=100,
        facecolor='white'
    )

    im = ax.imshow(
        ndvi_display,
        cmap=NDVI_COLORMAP,
        vmin=display_min,
        vmax=display_max,
        interpolation='bilinear'  # Smoother appearance
    )
    
    # Optional: Add colorbar
    # cbar = plt.colorbar(im, ax=ax, fraction=0.046, pad=0.04)
    # cbar.set_label('NDVI', rotation=270, labelpad=15)
    
    ax.axis("off")

    # Save to BytesIO buffer instead of disk
    buffer = io.BytesIO()
    fig.savefig(
        buffer,
        format='png',
        bbox_inches="tight",
        pad_inches=0.1,
        transparent=False,
        dpi=100
    )
    plt.close(fig)
    
    buffer.seek(0)  # Reset buffer position

    # --------------------------------------------------
    # 4. Upload to Supabase Storage
    # --------------------------------------------------
    try:
        # File path in bucket: tenant_id/land_id/YYYY-MM-DD_ndvi.png
        from datetime import date
        today = date.today().isoformat()
        
        file_path = f"{tenant_id}/{land_id}/{today}_ndvi.png"
        
        # Upload to Supabase Storage
        response = supabase.storage.from_(bucket_name).upload(
            path=file_path,
            file=buffer.getvalue(),
            file_options={
                "content-type": "image/png",
                "cache-control": "3600",
                "upsert": "true"  # Overwrite if exists
            }
        )
        
        # Get public URL
        public_url = supabase.storage.from_(bucket_name).get_public_url(file_path)
        
        logger.info(f"NDVI thumbnail uploaded: {file_path}")

    except Exception as e:
        logger.error(f"Supabase upload failed for land {land_id}: {e}")
        
        # Fallback: Save locally
        local_dir = os.path.join("thumbnails", "ndvi")
        os.makedirs(local_dir, exist_ok=True)
        local_path = os.path.join(local_dir, f"{land_id}.png")
        
        buffer.seek(0)
        with open(local_path, 'wb') as f:
            f.write(buffer.getvalue())
        
        public_url = f"/thumbnails/ndvi/{land_id}.png"
        logger.warning(f"Using local fallback: {local_path}")

    # --------------------------------------------------
    # 5. Metadata dictionary
    # --------------------------------------------------
    metadata = {
        "land_id": land_id,
        "tenant_id": tenant_id,
        "crs": "EPSG:4326",
        "bounds": {
            "north": float(north),
            "south": float(south),
            "east": float(east),
            "west": float(west)
        },
        "ndvi_range": {
            "min": float(display_min),
            "max": float(display_max),
            "actual_min": float(np.nanmin(valid_ndvi)) if len(valid_ndvi) > 0 else None,
            "actual_max": float(np.nanmax(valid_ndvi)) if len(valid_ndvi) > 0 else None,
        },
        "resolution": {
            "width": int(width),
            "height": int(height)
        },
        "generated_at": today
    }

    # Optional: Save metadata JSON to storage
    try:
        json_path = f"{tenant_id}/{land_id}/{today}_ndvi_metadata.json"
        json_buffer = io.BytesIO(json.dumps(metadata, indent=2).encode('utf-8'))
        
        supabase.storage.from_(bucket_name).upload(
            path=json_path,
            file=json_buffer.getvalue(),
            file_options={
                "content-type": "application/json",
                "upsert": "true"
            }
        )
    except Exception as e:
        logger.warning(f"Metadata JSON upload failed: {e}")

    return public_url, metadata
