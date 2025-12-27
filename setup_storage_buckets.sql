-- =====================================================
-- Supabase Storage Buckets Setup
-- =====================================================
-- Run this to create storage buckets for NDVI files
-- These commands should be run in Supabase Dashboard > Storage

-- =====================================================
-- 1. Create Buckets
-- =====================================================

-- NDVI Thumbnails Bucket (PNG + JSON metadata)
-- Run this in Supabase Dashboard > Storage > New Bucket
-- Bucket name: ndvi-thumbnails
-- Public bucket: YES
-- File size limit: 5MB
-- Allowed MIME types: image/png, application/json

-- NDVI Rasters Bucket (GeoTIFF files)
-- Run this in Supabase Dashboard > Storage > New Bucket
-- Bucket name: ndvi-rasters
-- Public bucket: YES
-- File size limit: 50MB
-- Allowed MIME types: image/tiff, image/geotiff

-- =====================================================
-- 2. Storage Policies (SQL Editor)
-- =====================================================

-- Allow public read access to ndvi-thumbnails
CREATE POLICY IF NOT EXISTS "Public read access to ndvi-thumbnails"
ON storage.objects
FOR SELECT
TO public
USING (bucket_id = 'ndvi-thumbnails');

-- Allow service role to upload/update/delete in ndvi-thumbnails
CREATE POLICY IF NOT EXISTS "Service role can manage ndvi-thumbnails"
ON storage.objects
FOR ALL
TO service_role
USING (bucket_id = 'ndvi-thumbnails')
WITH CHECK (bucket_id = 'ndvi-thumbnails');

-- Allow public read access to ndvi-rasters
CREATE POLICY IF NOT EXISTS "Public read access to ndvi-rasters"
ON storage.objects
FOR SELECT
TO public
USING (bucket_id = 'ndvi-rasters');

-- Allow service role to upload/update/delete in ndvi-rasters
CREATE POLICY IF NOT EXISTS "Service role can manage ndvi-rasters"
ON storage.objects
FOR ALL
TO service_role
USING (bucket_id = 'ndvi-rasters')
WITH CHECK (bucket_id = 'ndvi-rasters');

-- =====================================================
-- 3. Add GeoTIFF URL Column to Lands Table (if not exists)
-- =====================================================

-- Add column for GeoTIFF URL in lands table
ALTER TABLE lands 
ADD COLUMN IF NOT EXISTS ndvi_geotiff_url TEXT;

COMMENT ON COLUMN lands.ndvi_geotiff_url IS 
'Public URL to full-resolution NDVI GeoTIFF in Supabase Storage';

-- =====================================================
-- Verification Queries
-- =====================================================

-- Check if buckets exist
-- SELECT * FROM storage.buckets WHERE name IN ('ndvi-thumbnails', 'ndvi-rasters');

-- Check bucket policies
-- SELECT * FROM storage.objects WHERE bucket_id IN ('ndvi-thumbnails', 'ndvi-rasters') LIMIT 10;

-- Check if column was added
-- SELECT column_name, data_type FROM information_schema.columns 
-- WHERE table_name = 'lands' AND column_name = 'ndvi_geotiff_url';
