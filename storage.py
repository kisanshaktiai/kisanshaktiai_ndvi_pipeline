"""
storage.py
----------

Supabase Storage upload utilities for NDVI pipeline.
Handles thumbnail and GeoTIFF uploads to cloud storage.
"""

import os
from typing import Optional
from supabase import Client
from logger import logger


# --------------------------------------------------
# Supabase Storage Configuration
# --------------------------------------------------
NDVI_THUMBNAILS_BUCKET = "ndvi-thumbnails"
NDVI_RASTERS_BUCKET = "ndvi-rasters"


# --------------------------------------------------
# Upload file to Supabase Storage
# --------------------------------------------------
def upload_to_supabase(
    supabase: Client,
    bucket_name: str,
    file_path: str,
    destination_path: str,
    content_type: str = "image/png",
) -> Optional[str]:
    """
    Upload file to Supabase Storage and return public URL.
    
    Args:
        supabase: Supabase client instance
        bucket_name: Storage bucket name
        file_path: Local file path to upload
        destination_path: Destination path in bucket (e.g., "land_id.png")
        content_type: MIME type of file
        
    Returns:
        Public URL if successful, None if failed
    """
    try:
        # Read file content
        with open(file_path, "rb") as f:
            file_content = f.read()
        
        # Upload to Supabase Storage
        supabase.storage.from_(bucket_name).upload(
            path=destination_path,
            file=file_content,
            file_options={
                "content-type": content_type,
                "upsert": "true"  # Overwrite if exists
            }
        )
        
        # Get public URL
        public_url = supabase.storage.from_(bucket_name).get_public_url(
            destination_path
        )
        
        logger.info(f"Uploaded to Supabase Storage: {destination_path}")
        return public_url
        
    except Exception as e:
        logger.error(f"Failed to upload {file_path} to Supabase Storage: {e}")
        return None


# --------------------------------------------------
# Upload NDVI thumbnail (PNG + JSON)
# --------------------------------------------------
def upload_ndvi_thumbnail(
    supabase: Client,
    land_id: str,
    png_path: str,
    json_path: str,
) -> Optional[str]:
    """
    Upload NDVI thumbnail PNG and metadata JSON to Supabase Storage.
    
    Returns:
        Public URL to PNG if successful
    """
    
    # Upload PNG
    png_url = upload_to_supabase(
        supabase=supabase,
        bucket_name=NDVI_THUMBNAILS_BUCKET,
        file_path=png_path,
        destination_path=f"{land_id}.png",
        content_type="image/png"
    )
    
    if not png_url:
        return None
    
    # Upload JSON metadata
    upload_to_supabase(
        supabase=supabase,
        bucket_name=NDVI_THUMBNAILS_BUCKET,
        file_path=json_path,
        destination_path=f"{land_id}.json",
        content_type="application/json"
    )
    
    return png_url


# --------------------------------------------------
# Upload NDVI GeoTIFF
# --------------------------------------------------
def upload_ndvi_geotiff(
    supabase: Client,
    land_id: str,
    geotiff_path: str,
) -> Optional[str]:
    """
    Upload NDVI GeoTIFF to Supabase Storage.
    
    Returns:
        Public URL to GeoTIFF if successful
    """
    
    return upload_to_supabase(
        supabase=supabase,
        bucket_name=NDVI_RASTERS_BUCKET,
        file_path=geotiff_path,
        destination_path=f"{land_id}_ndvi.tif",
        content_type="image/tiff"
    )


# --------------------------------------------------
# Clean up local files after upload
# --------------------------------------------------
def cleanup_local_file(file_path: str) -> None:
    """
    Delete local file after successful upload.
    """
    try:
        if os.path.exists(file_path):
            os.remove(file_path)
            logger.debug(f"Cleaned up local file: {file_path}")
    except Exception as e:
        logger.warning(f"Failed to cleanup local file {file_path}: {e}")
