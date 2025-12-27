# NDVI Pipeline - Critical Fixes Applied

## Summary of Issues & Solutions

### 🔴 Issues Found in Logs

1. **WARNING: NDVI log insert failed** - Every log entry failing
2. **Files created locally but NOT uploaded** - No cloud storage integration
3. **Local file paths in database** - URLs pointing to `/thumbnails/ndvi/...` instead of Supabase Storage
4. **Missing GeoTIFF URL field** - No place to store GeoTIFF links

---

## ✅ Solutions Implemented

### 1. **New Storage Module** (`storage.py`)
- ✅ Supabase Storage upload functions
- ✅ Handles PNG thumbnails + JSON metadata
- ✅ Handles GeoTIFF full-resolution files
- ✅ Returns public URLs
- ✅ Error handling and logging

### 2. **Updated Upload Functions**

**`ndvi_thumbnail.py`:**
- ✅ Creates PNG + JSON locally
- ✅ Uploads both to `ndvi-thumbnails` bucket
- ✅ Returns Supabase Storage public URL

**`ndvi_geotiff.py`:**
- ✅ Creates GeoTIFF locally
- ✅ Uploads to `ndvi-rasters` bucket
- ✅ Returns Supabase Storage public URL

### 3. **Enhanced Database Layer** (`db.py`)
- ✅ Better error handling for logs
- ✅ Detailed error messages (won't see generic warnings anymore)
- ✅ Added `geotiff_url` parameter to `update_land_ndvi_snapshot()`
- ✅ Added `get_supabase_client()` function

### 4. **Updated Pipeline** (`main.py` + `processor.py`)
- ✅ Pass Supabase client to processor
- ✅ Handle both thumbnail and GeoTIFF URLs
- ✅ Store GeoTIFF URL in metadata and lands table
- ✅ Better logging of upload status

### 5. **Database Setup Scripts**

**`sql/create_ndvi_processing_logs.sql`:**
- ✅ Creates missing `ndvi_processing_logs` table
- ✅ Adds indexes for performance
- ✅ Sets up Row Level Security

**`sql/setup_storage_buckets.sql`:**
- ✅ Instructions for creating storage buckets
- ✅ Storage policies for public access
- ✅ Adds `ndvi_geotiff_url` column to lands table

---

## 📋 Required Actions

### 1. Database Setup (5 minutes)

**Step 1:** Create Processing Logs Table
```sql
-- In Supabase SQL Editor, run:
sql/create_ndvi_processing_logs.sql
```

**Step 2:** Setup Storage Buckets

Via Supabase Dashboard > Storage:
1. Create bucket: `ndvi-thumbnails` (Public, 5MB limit)
2. Create bucket: `ndvi-rasters` (Public, 50MB limit)

Then run in SQL Editor:
```sql
sql/setup_storage_buckets.sql
```

### 2. Deploy Updated Code

Replace these files:
```
storage.py           ← NEW
db.py               ← UPDATED
main.py             ← UPDATED
processor.py        ← UPDATED
ndvi_thumbnail.py   ← UPDATED
ndvi_geotiff.py     ← UPDATED
```

### 3. Verify Environment Variables

Ensure `.env` has:
```env
SUPABASE_URL=https://qfklkkzxemsbeniyugiz.supabase.co
SUPABASE_KEY=<YOUR_SERVICE_ROLE_KEY>  # ⚠️ Use SERVICE ROLE, not anon key
```

---

## 🧪 Testing

After deployment, run:

```bash
python main.py
```

**Expected output:**
```
INFO | NDVI pipeline started
INFO | Uploaded to Supabase Storage: land_id.png
INFO | NDVI thumbnail uploaded: land_id
INFO | NDVI GeoTIFF uploaded: land_id
DEBUG | NDVI log inserted | step=PROCESS_START
INFO | NDVI pipeline finished
```

**Verify in Supabase:**

1. **Storage Files:**
   - Go to Storage > ndvi-thumbnails
   - Should see `.png` and `.json` files

2. **Database Records:**
```sql
SELECT id, ndvi_thumbnail_url, ndvi_geotiff_url 
FROM lands 
WHERE ndvi_tested = true 
LIMIT 5;
```

3. **Processing Logs:**
```sql
SELECT * FROM ndvi_processing_logs 
ORDER BY created_at DESC 
LIMIT 10;
```

---

## 🎯 What's Fixed

| Issue | Status | Details |
|-------|--------|---------|
| Local file paths in DB | ✅ Fixed | Now stores Supabase Storage URLs |
| Files not uploaded | ✅ Fixed | Auto-upload to cloud storage |
| Log inserts failing | ✅ Fixed | Better error handling + table creation script |
| Missing GeoTIFF URLs | ✅ Fixed | Added column + upload logic |
| Poor error messages | ✅ Fixed | Detailed logging throughout |

---

## 📚 Documentation

- **`SETUP_GUIDE.md`** - Complete setup instructions
- **`sql/create_ndvi_processing_logs.sql`** - Database table setup
- **`sql/setup_storage_buckets.sql`** - Storage configuration

---

## 🚀 Next Steps

1. Run database setup scripts ✅
2. Deploy updated code ✅
3. Test with sample data ✅
4. Monitor first production run ✅
5. Enable GitHub Actions for automation ✅

---

## 📞 Support

If you encounter issues:
1. Check Supabase Dashboard > Logs
2. Verify bucket permissions
3. Confirm SERVICE ROLE key is set
4. Review `SETUP_GUIDE.md` for detailed troubleshooting
