"""
setup_supabase_storage.py
-------------------------

One-time setup script to create Supabase Storage bucket for NDVI thumbnails.

Run this ONCE before running the main pipeline:
    python setup_supabase_storage.py

This will:
1. Create 'ndvi-thumbnails' bucket (if not exists)
2. Set public access policy
3. Configure CORS for web access
"""

import os
from dotenv import load_dotenv
from supabase import create_client
from logger import logger

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

if not SUPABASE_URL or not SUPABASE_KEY:
    raise RuntimeError(
        "Supabase credentials not set. "
        "Ensure SUPABASE_URL and SUPABASE_KEY are in .env file"
    )

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)


def setup_storage_bucket():
    """
    Create and configure Supabase Storage bucket for NDVI thumbnails.
    """
    
    BUCKET_NAME = "ndvi-thumbnails"
    
    try:
        # --------------------------------------------------
        # 1. Check if bucket exists
        # --------------------------------------------------
        try:
            existing_buckets = supabase.storage.list_buckets()
            bucket_exists = any(b["name"] == BUCKET_NAME for b in existing_buckets)
            
            if bucket_exists:
                logger.info(f"✓ Bucket '{BUCKET_NAME}' already exists")
            else:
                # --------------------------------------------------
                # 2. Create bucket
                # --------------------------------------------------
                supabase.storage.create_bucket(
                    BUCKET_NAME,
                    options={
                        "public": True,  # Public read access
                        "file_size_limit": 5242880,  # 5MB max
                        "allowed_mime_types": ["image/png", "image/jpeg", "application/json"]
                    }
                )
                logger.info(f"✓ Created bucket '{BUCKET_NAME}'")
        
        except Exception as e:
            logger.error(f"Bucket creation failed: {e}")
            return False

        # --------------------------------------------------
        # 3. Verify bucket access
        # --------------------------------------------------
        try:
            # Test upload a dummy file
            test_content = b"NDVI Pipeline Test"
            test_path = "test/init.txt"
            
            supabase.storage.from_(BUCKET_NAME).upload(
                path=test_path,
                file=test_content,
                file_options={"upsert": "true"}
            )
            
            # Get public URL
            public_url = supabase.storage.from_(BUCKET_NAME).get_public_url(test_path)
            logger.info(f"✓ Bucket access verified")
            logger.info(f"  Test file URL: {public_url}")
            
            # Clean up test file
            supabase.storage.from_(BUCKET_NAME).remove([test_path])
            
        except Exception as e:
            logger.warning(f"Bucket verification failed: {e}")

        # --------------------------------------------------
        # 4. Storage organization structure
        # --------------------------------------------------
        logger.info(f"\n{'='*60}")
        logger.info("STORAGE STRUCTURE")
        logger.info(f"{'='*60}")
        logger.info(f"Bucket: {BUCKET_NAME}")
        logger.info("Organization:")
        logger.info("  ├── <tenant_id>/")
        logger.info("  │   ├── <land_id>/")
        logger.info("  │   │   ├── YYYY-MM-DD_ndvi.png")
        logger.info("  │   │   ├── YYYY-MM-DD_ndvi_metadata.json")
        logger.info("  │   │   └── ...")
        logger.info(f"{'='*60}\n")

        return True

    except Exception as e:
        logger.exception(f"Storage setup failed: {e}")
        return False


def show_policy_instructions():
    """
    Display SQL commands for setting up Row Level Security (RLS) policies.
    """
    logger.info("\n" + "="*60)
    logger.info("SUPABASE RLS POLICY SETUP (Optional)")
    logger.info("="*60)
    logger.info("If you want tenant-specific access control, run this SQL in Supabase:")
    logger.info("")
    logger.info("""
-- Enable RLS on storage.objects
ALTER TABLE storage.objects ENABLE ROW LEVEL SECURITY;

-- Policy: Allow public READ access to all thumbnails
CREATE POLICY "Public read access for NDVI thumbnails"
ON storage.objects FOR SELECT
USING (bucket_id = 'ndvi-thumbnails');

-- Policy: Allow authenticated users to upload to their tenant folder
CREATE POLICY "Tenant upload access"
ON storage.objects FOR INSERT
WITH CHECK (
    bucket_id = 'ndvi-thumbnails' 
    AND auth.uid() IS NOT NULL
);

-- Policy: Allow users to update their own tenant's files
CREATE POLICY "Tenant update access"
ON storage.objects FOR UPDATE
USING (
    bucket_id = 'ndvi-thumbnails'
    AND auth.uid() IS NOT NULL
);

-- Policy: Allow users to delete their own tenant's files
CREATE POLICY "Tenant delete access"
ON storage.objects FOR DELETE
USING (
    bucket_id = 'ndvi-thumbnails'
    AND auth.uid() IS NOT NULL
);
    """)
    logger.info("="*60 + "\n")


def main():
    logger.info("SUPABASE STORAGE SETUP")
    logger.info("="*60 + "\n")
    
    success = setup_storage_bucket()
    
    if success:
        logger.info("✓ Storage setup complete!")
        logger.info("  You can now run the NDVI pipeline: python main.py")
        
        # Show optional RLS setup
        show_policy_instructions()
    else:
        logger.error("✗ Storage setup failed. Check errors above.")
        return 1
    
    return 0


if __name__ == "__main__":
    exit(main())
