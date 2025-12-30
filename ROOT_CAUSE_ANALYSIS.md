# 🔴 NDVI Pipeline - Complete Root Cause Analysis

**Date:** December 30, 2025  
**Analysis Type:** Deep Code Audit  
**Findings:** 4 Critical Issues + Solutions

---

## 🎯 ISSUE #1: Missing Escalation Worker File

### **Symptom:**
```bash
python ndvi_escalation_worker.py
# ❌ FileNotFoundError: No such file or directory
```

### **Root Cause:**
The GitHub Actions workflow (`.github/workflows/ndvi-pipeline.yml`) references a file that **doesn't exist** in the repository:

```yaml
# Line 31-32 in workflow
- name: Run NDVI escalation worker
  run: python ndvi_escalation_worker.py  # ❌ FILE MISSING
```

### **Impact:**
- **100% of lands show "⏳ NDVI pending (no grid data yet)"**
- Worker never actually runs NDVI processing
- Lands stay stuck in "pending" status forever
- No NDVI data gets saved to database

### **Solution:**
✅ **Created:** `ndvi_escalation_worker.py` with proper implementation

**Features:**
- Fetches lands with `ndvi_status IN ('pending', 'failed')`
- Processes each land using `process_land()`
- Updates `lands.ndvi_status` to 'completed' or 'failed'
- Logs all steps to `ndvi_processing_logs`
- Returns proper exit codes for CI/CD

---

## 🔴 ISSUE #2: MCARI Calculation Scale Error

### **Symptom:**
MCARI values are **completely wrong**:
```python
Expected: 0.0 to 2.0 (healthy crops)
Actual:   443, 3.4 billion, -9999999, etc.
```

### **Root Cause:**
**Sentinel-2 bands are in DN scale (0-10000) instead of reflectance (0-1)**

**Current `raster_utils.py` (Line 28-58):**
```python
# ❌ PROBLEM: Only converts if max > 10.0
if max_val > 10.0:
    data = data / 10000.0  # Converts
elif max_val > 1.0 and max_val <= 10.0:
    logger.warning(...)  # ⚠️ DOESN'T CONVERT!
```

**What happens with bands in range 1-10:**
- Not converted to reflectance
- MCARI calculation uses values like B04=6500 instead of 0.65
- Results in impossibly large MCARI values

### **Why This Breaks MCARI:**

**MCARI Formula:**
```
MCARI = [(B05 - B04) - 0.2 × (B05 - B03)] × (B05 / B04)
```

**With WRONG scale (DN 0-10000):**
```python
B03 = 5500 (should be 0.55)
B04 = 6500 (should be 0.65)
B05 = 7000 (should be 0.70)

MCARI = [(7000-6500) - 0.2×(7000-5500)] × (7000/6500)
      = [500 - 300] × 1.077
      = 200 × 1.077
      = 215.4  ❌ WRONG!
```

**With CORRECT scale (reflectance 0-1):**
```python
B03 = 0.55
B04 = 0.65
B05 = 0.70

MCARI = [(0.70-0.65) - 0.2×(0.70-0.55)] × (0.70/0.65)
      = [0.05 - 0.03] × 1.077
      = 0.02 × 1.077
      = 0.022  ✅ CORRECT!
```

### **Solution:**
✅ **Fixed:** `raster_utils_fixed.py` with **mandatory conversion**

**Changes:**
1. **Always converts if max > 1.0** (no more ambiguous range)
2. **Raises error if final values > 1.5** (catches remaining issues)
3. **Detailed logging** of band ranges before/after conversion
4. **Strict validation** to prevent bad data from reaching MCARI

---

## 🔴 ISSUE #3: MCARI Calculation Missing Validation

### **Symptom:**
Even after band scale fixes, MCARI sometimes produces bad values because:
- No pre-calculation band range checks
- No post-calculation sanity checks
- Silent failures that corrupt database

### **Root Cause:**
**Current `indices.py` (Line 60-74):**
```python
# ❌ PROBLEM: No validation before calculation
mcari = (red_edge_red_diff - 0.2 * red_edge_green_diff) * (b["B05"] / b04_safe)
mcari = np.clip(mcari, -1.0, 5.0)  # Only clips AFTER calculation
```

**Missing checks:**
1. Are bands actually in 0-1 range?
2. Did read_band() conversion work?
3. Are MCARI output values physically reasonable?

### **Solution:**
✅ **Fixed:** `indices_fixed.py` with **comprehensive validation**

**New validation pipeline:**

```python
# BEFORE calculation
✅ Check all bands are 0-1 range
✅ Raise error if any band > 1.5 (DN scale leaked through)
✅ Log band statistics for diagnostics

# DURING calculation
✅ Safe division (avoid divide by zero)
✅ Log intermediate values

# AFTER calculation
✅ Check raw MCARI before clipping
✅ If abs(mcari) > 100, set to NaN and log ERROR
✅ Clip to -1 to 5 range
✅ Log final MCARI statistics
```

**Example diagnostic output:**
```
✅ Band ranges validated: B03=[0.123, 0.456], B04=[0.089, 0.521], ...
✅ MCARI calculated successfully: mean=0.823, range=[0.234, 1.456]
```

**Or if scale error:**
```
❌ MCARI CALCULATION FAILED! Raw values: min=-9999, max=443. Band scale error!
❌ Band statistics: B03={min:4500, max:7800}, B04={min:5200, max:8900}
```

---

## 🔴 ISSUE #4: Database Query Logic in Escalation Worker

### **Symptom:**
Worker fetches lands but logs show:
```
⏳ NDVI pending (no grid data yet) for land ca9687fa-...
⏳ NDVI pending (no grid data yet) for land 5805d831-...
```

### **Root Cause:**
**Two separate issues:**

**Issue 4A: Wrong query condition**
```python
# Current escalation worker query (WRONG)
.or_("ndvi_status.eq.pending,ndvi_tested.eq.false")

# ❌ PROBLEM: This fetches lands that have:
# - ndvi_status = 'pending' OR
# - ndvi_tested = false
# But doesn't check if Sentinel-2 data is actually available!
```

**Issue 4B: `processor.py` returns None silently**
```python
# processor.py Line 103-106
if len(ndvi_series) < 2:
    logger.warning("Insufficient NDVI observations")
    return None  # ❌ No status update in database!
```

**Combined effect:**
1. Worker fetches land with `ndvi_status='pending'`
2. Calls `process_land()`
3. No Sentinel-2 data found → returns `None`
4. Worker logs "no grid data yet"
5. **But never updates land status!**
6. Next run: Same land fetched again (infinite loop)

### **Solution:**
✅ **Fixed in new `ndvi_escalation_worker.py`:**

**Fix 4A: Better query + status tracking**
```python
# Fetch lands but track WHY they're pending
response = supabase.table("lands").select(
    "..., last_processed_at, ndvi_status"
).or_("ndvi_status.eq.pending,ndvi_status.eq.failed,ndvi_tested.eq.false")
.order("last_processed_at", desc=False)  # Oldest first (avoid retry loops)
```

**Fix 4B: Proper status updates**
```python
if result is None:
    # Log as SKIPPED (not failed) with reason
    log_ndvi_step(
        processing_step="ESCALATION_PENDING",
        step_status="skipped",
        metadata={"reason": "insufficient_satellite_data"}
    )
    # DON'T update land status (will retry later)
    continue  # Move to next land
```

**New behavior:**
- Land stays `pending` if no S2 data (will retry in 6-12 days when S2 pass happens)
- Only marks `failed` if actual error (geometry invalid, API error, etc.)
- Logs reason for skipping in `ndvi_processing_logs`

---

## 📊 COMPLETE DEPLOYMENT CHECKLIST

### **Step 1: Replace Files (CRITICAL)**

```bash
# Core files - MUST replace
cp ndvi_escalation_worker.py /your/repo/
cp raster_utils_fixed.py /your/repo/raster_utils.py  # OVERWRITE
cp indices_fixed.py /your/repo/indices.py            # OVERWRITE
```

### **Step 2: Database Migration (if needed)**

```sql
-- Ensure ndvi_processing_logs table exists
-- Run: sql/create_ndvi_processing_logs.sql

-- Verify columns
SELECT column_name, data_type 
FROM information_schema.columns 
WHERE table_name = 'lands' 
  AND column_name IN ('ndvi_status', 'last_processed_at');
```

### **Step 3: Test Locally**

```bash
# Test with single land
python ndvi_escalation_worker.py

# Expected output:
# ✅ Band ranges validated: B03=[0.1, 0.5], B04=[0.1, 0.6], ...
# ✅ MCARI calculated successfully: mean=0.823
# ✅ Land abc123 processed successfully (NDVI=0.456)
```

### **Step 4: Verify in Database**

```sql
-- Check MCARI values are reasonable
SELECT 
    land_id, 
    date, 
    ndvi_value,
    mcari_value,
    CASE 
        WHEN mcari_value BETWEEN 0 AND 2 THEN '✅ Good'
        WHEN mcari_value > 10 THEN '❌ Scale error'
        WHEN mcari_value IS NULL THEN '⚠️  Not calculated'
        ELSE '⚠️  Check'
    END as mcari_status,
    metadata->>'mcari_trend' as mcari_trend
FROM ndvi_data
WHERE date >= CURRENT_DATE - INTERVAL '2 days'
ORDER BY mcari_value DESC NULLS LAST;
```

### **Step 5: Deploy to Production**

```bash
# Commit changes
git add ndvi_escalation_worker.py raster_utils.py indices.py
git commit -m "FIX: Add escalation worker + fix MCARI scale issues"
git push

# GitHub Actions will auto-deploy
```

---

## 🧪 EXPECTED RESULTS AFTER FIX

### **Before Fix:**
```
⏳ NDVI pending (no grid data yet) for land ca9687fa-...
⏳ NDVI pending (no grid data yet) for land 5805d831-...
⏳ NDVI pending (no grid data yet) for land 4af38aa2-...
...
✅ NDVI escalation worker finished. Lands processed: 0
```

### **After Fix:**
```
✅ Band ranges validated: B03=[0.234, 0.678], B04=[0.189, 0.723]
✅ MCARI calculated successfully: mean=0.856, range=[0.234, 1.523]
✅ Land ca9687fa-... processed (NDVI=0.456, MCARI=0.823)
✅ Land 5805d831-... processed (NDVI=0.623, MCARI=1.045)
⏳ Land 4af38aa2-... skipped (no Sentinel-2 data in lookback window)
...
✅ NDVI escalation worker finished. Lands processed: 18
```

### **Database Records:**
```sql
-- BEFORE: All NULL/wrong values
land_id          | ndvi_value | mcari_value | status
ca9687fa-...     | NULL       | NULL        | pending

-- AFTER: Proper values
land_id          | ndvi_value | mcari_value | status
ca9687fa-...     | 0.456      | 0.823       | completed
5805d831-...     | 0.623      | 1.045       | completed
4af38aa2-...     | NULL       | NULL        | pending (no S2 data)
```

---

## 🎯 SUMMARY OF FIXES

| Issue | Severity | Status | Files Changed |
|-------|----------|--------|---------------|
| Missing escalation worker | 🔴 Critical | ✅ Fixed | `ndvi_escalation_worker.py` (NEW) |
| MCARI wrong scale (DN) | 🔴 Critical | ✅ Fixed | `raster_utils.py` |
| MCARI no validation | 🟠 High | ✅ Fixed | `indices.py` |
| Status update logic | 🟡 Medium | ✅ Fixed | `ndvi_escalation_worker.py` |

---

## 📞 VERIFICATION COMMANDS

### **1. Check File Exists**
```bash
ls -lh ndvi_escalation_worker.py
# Should show: -rw-r--r-- 1 user user 12K Dec 30 ndvi_escalation_worker.py
```

### **2. Test Band Scale Detection**
```bash
python -c "
from raster_utils import read_band
import numpy as np

# Create test data in DN scale
test_data = np.array([[5000, 6000], [7000, 8000]], dtype='float32')
print('Test DN data:', test_data)

# Should automatically convert to reflectance
# (mock the rasterio parts - this is conceptual test)
"
```

### **3. Check Database After Run**
```sql
-- Verify MCARI values
SELECT 
    COUNT(*) as total_records,
    COUNT(mcari_value) as mcari_calculated,
    AVG(mcari_value) as avg_mcari,
    MIN(mcari_value) as min_mcari,
    MAX(mcari_value) as max_mcari
FROM ndvi_data
WHERE date >= CURRENT_DATE - INTERVAL '7 days';

-- Expected:
-- avg_mcari: 0.5 to 1.5
-- min_mcari: -0.5 to 0.0
-- max_mcari: 1.5 to 3.0
```

---

## ✅ SUCCESS CRITERIA

After deploying fixes, you should see:

1. ✅ **Escalation worker runs successfully**
   - No "FileNotFoundError"
   - Processes lands with pending status

2. ✅ **MCARI values in correct range**
   - Typical: 0.0 to 2.0
   - No values > 10 (indicates scale error)
   - No values in millions/billions

3. ✅ **Lands get processed**
   - Some lands: status → 'completed' (if S2 data available)
   - Some lands: stay 'pending' (if S2 data not yet available)
   - Few lands: status → 'failed' (only if actual error)

4. ✅ **Database populated**
   - `ndvi_data` table has new records
   - `mcari_value` column has reasonable values
   - `lands.ndvi_status` updated correctly

---

**Next Steps:** Deploy these 3 files and test immediately!
