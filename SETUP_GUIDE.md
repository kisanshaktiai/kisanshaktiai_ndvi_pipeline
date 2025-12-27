# NDVI Pipeline Setup & Deployment Guide

## Issues Fixed

### 1. ✅ File Upload to Supabase Storage
**Problem:** Files were created locally but not uploaded to Supabase Storage
**Solution:** 
- Created new `storage.py` module with Supabase Storage integration
- Updated `ndvi_thumbnail.py` to upload PNG + JSON to cloud storage
- Updated `ndvi_geotiff.py` to upload GeoTIFF to cloud storage
- Modified `processor.py` to pass Supabase client to upload functions

### 2. ✅ Processing Logs Failing
**Problem:** All `log_ndvi_step()` calls were failing silently
**Solution:**
- Enhanced error handling in `db.py` with detailed logging
- Created SQL script to create `ndvi_processing_logs` table
- Added graceful degradation (logs won't break pipeline if table missing)

### 3. ✅ Missing GeoTIFF URL Field
**Problem:** No field in database to store GeoTIFF URLs
**Solution:**
- Added `ndvi_geotiff_url` column to lands table
- Updated `update_land_ndvi_snapshot()` to accept GeoTIFF URL
- Modified `main.py` to store GeoTIFF URL in both lands table and metadata

---

## Step-by-Step Setup

### Step 1: Database Setup

#### 1.1 Create Processing Logs Table
Run this in **Supabase SQL Editor**:

```bash
# Upload and run sql/create_ndvi_processing_logs.sql
```

This creates:
- `ndvi_processing_logs` table with proper indexes
- Row Level Security policies
- Performance indexes for queries

#### 1.2 Setup Storage Buckets

**Via Supabase Dashboard:**

1. Go to **Storage** > **New Bucket**
2. Create bucket: `ndvi-thumbnails`
   - Public: ✅ YES
   - File size limit: 5MB
   - Allowed MIME: `image/png`, `application/json`

3. Create bucket: `ndvi-rasters`
   - Public: ✅ YES
   - File size limit: 50MB
   - Allowed MIME: `image/tiff`, `image/geotiff`

**Then run in SQL Editor:**

```bash
# Upload and run sql/setup_storage_buckets.sql
```

This creates:
- Storage bucket policies for public read access
- Service role upload permissions
- `ndvi_geotiff_url` column in lands table

### Step 2: Update Environment Variables

Ensure your `.env` file has:

```env
SUPABASE_URL=https://qfklkkzxemsbeniyugiz.supabase.co
SUPABASE_KEY=your_service_role_key_here  # Use SERVICE ROLE key, not anon key
```

**Important:** Use the **Service Role** key (not anon key) for storage uploads.

### Step 3: Deploy Updated Code

Replace these files in your repository:

```bash
# Core modules
storage.py          # NEW - Supabase Storage integration
db.py              # UPDATED - Better error handling, GeoTIFF URL support
main.py            # UPDATED - Pass Supabase client, handle GeoTIFF URLs
processor.py       # UPDATED - Upload files to storage
ndvi_thumbnail.py  # UPDATED - Upload to Supabase Storage
ndvi_geotiff.py    # UPDATED - Upload to Supabase Storage

# SQL scripts
sql/create_ndvi_processing_logs.sql  # NEW
sql/setup_storage_buckets.sql        # NEW
```

### Step 4: Install Dependencies

No new dependencies required - everything uses existing packages.

### Step 5: Test Locally

```bash
python main.py
```

**Expected Output:**
```
INFO | NDVI pipeline started
INFO | Uploaded to Supabase Storage: land_id.png
INFO | NDVI thumbnail uploaded: land_id
INFO | NDVI GeoTIFF created locally: rasters/ndvi/land_id_ndvi.tif
INFO | Uploaded to Supabase Storage: land_id_ndvi.tif
INFO | NDVI GeoTIFF uploaded: land_id
DEBUG | NDVI log inserted | step=PROCESS_START | status=started
INFO | NDVI pipeline finished
```

### Step 6: Verify in Supabase

#### Check Storage Buckets
```sql
-- View uploaded files
SELECT name, bucket_id, created_at, metadata 
FROM storage.objects 
WHERE bucket_id IN ('ndvi-thumbnails', 'ndvi-rasters')
ORDER BY created_at DESC 
LIMIT 20;
```

#### Check Processing Logs
```sql
-- View recent processing logs
SELECT processing_step, step_status, land_id, duration_ms, created_at
FROM ndvi_processing_logs
ORDER BY created_at DESC
LIMIT 20;
```

#### Check Land Records
```sql
-- Verify URLs are saved
SELECT id, last_ndvi_value, ndvi_thumbnail_url, ndvi_geotiff_url, ndvi_status
FROM lands
WHERE ndvi_tested = true
ORDER BY last_processed_at DESC
LIMIT 10;
```

---

## Architecture Changes

### File Flow (Before → After)

**Before:**
```
processor.py → local file → local path → database
                    ❌ Never uploaded to cloud
```

**After:**
```
processor.py → local file → Supabase Storage → public URL → database
                    ✅ Files accessible via URL
```

### New Data Flow

1. **Sentinel-2 Processing** → `processor.py`
2. **Generate Thumbnail** → `ndvi_thumbnail.py`
   - Create PNG locally
   - Upload to `ndvi-thumbnails` bucket
   - Return public URL
3. **Generate GeoTIFF** → `ndvi_geotiff.py`
   - Create TIF locally
   - Upload to `ndvi-rasters` bucket
   - Return public URL
4. **Save URLs** → `db.py`
   - `ndvi_thumbnail_url` → lands table
   - `ndvi_geotiff_url` → lands table
   - Both URLs → ndvi_data metadata

### Storage Structure

```
ndvi-thumbnails/
├── land_id_1.png
├── land_id_1.json
├── land_id_2.png
├── land_id_2.json
└── ...

ndvi-rasters/
├── land_id_1_ndvi.tif
├── land_id_2_ndvi.tif
└── ...
```

---

## Troubleshooting

### Issue: "NDVI log insert failed"
**Solution:** Run `sql/create_ndvi_processing_logs.sql` in Supabase SQL Editor

### Issue: "Failed to upload to Supabase Storage"
**Check:**
1. Buckets exist (`ndvi-thumbnails`, `ndvi-rasters`)
2. Using SERVICE ROLE key (not anon key)
3. Bucket policies are set correctly
4. Run `sql/setup_storage_buckets.sql`

### Issue: "Column ndvi_geotiff_url does not exist"
**Solution:** Run `sql/setup_storage_buckets.sql` to add the column

### Issue: Files uploaded but no public URL
**Check:**
1. Bucket is set to PUBLIC
2. Storage policies allow public SELECT
3. Run bucket policy setup from `sql/setup_storage_buckets.sql`

---

## Performance Considerations

### Local File Cleanup
Files are kept locally after upload. To enable automatic cleanup, uncomment in `storage.py`:

```python
from storage import cleanup_local_file

# After successful upload
cleanup_local_file(png_path)
cleanup_local_file(json_path)
```

### Batch Uploads
For large-scale processing (100+ lands), consider:
- Parallel uploads using `concurrent.futures`
- Rate limiting to avoid storage API limits
- Chunked processing with progress tracking

---

## Monitoring & Observability

### Query Processing Metrics
```sql
-- Average processing time per step
SELECT 
    processing_step,
    AVG(duration_ms) as avg_duration_ms,
    COUNT(*) as count,
    SUM(CASE WHEN step_status = 'failed' THEN 1 ELSE 0 END) as failures
FROM ndvi_processing_logs
WHERE created_at > NOW() - INTERVAL '24 hours'
GROUP BY processing_step
ORDER BY avg_duration_ms DESC;
```

### Check Upload Success Rate
```sql
-- Lands with missing file URLs
SELECT COUNT(*) as missing_thumbnails
FROM lands 
WHERE ndvi_tested = true 
  AND ndvi_thumbnail_url IS NULL;

SELECT COUNT(*) as missing_geotiffs
FROM lands 
WHERE ndvi_tested = true 
  AND ndvi_geotiff_url IS NULL;
```

---

## Next Steps

1. ✅ **Complete database setup** (run SQL scripts)
2. ✅ **Deploy updated code** (replace files)
3. ✅ **Test with sample data** (run locally)
4. ✅ **Verify in Supabase** (check storage + database)
5. 🚀 **Enable GitHub Actions** (automated daily runs)

---

## Support

If issues persist:
1. Check Supabase logs in Dashboard > Logs
2. Verify storage bucket configuration
3. Confirm SERVICE ROLE key is used
4. Check network/firewall settings for Supabase API access
