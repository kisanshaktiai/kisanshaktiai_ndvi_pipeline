# 🌾 NDVI Agricultural Monitoring Pipeline

**Automated satellite-based crop health monitoring using Sentinel-2 and Sentinel-1 data**

---

## 📋 Overview

This pipeline processes satellite imagery to generate NDVI (Normalized Difference Vegetation Index) and other vegetation health metrics for agricultural land parcels. It combines:

- **Sentinel-2 optical imagery** (10m resolution) for vegetation indices
- **Sentinel-1 SAR data** for all-weather soil moisture monitoring
- **Cloud masking** using Scene Classification Layer (SCL)
- **Multi-temporal analysis** for trend detection
- **Crop-specific health classification** with agronomic alerts
- **Automated thumbnail generation** and upload to Supabase Storage

---

## 🚀 Quick Start

### 1. Prerequisites

```bash
# Python 3.11+
python --version

# Install dependencies
pip install -r requirements.txt
```

### 2. Environment Setup

Create `.env` file:

```env
SUPABASE_URL=https://your-project.supabase.co
SUPABASE_KEY=your-service-role-key
```

### 3. Setup Supabase Storage

```bash
# One-time setup to create storage bucket
python setup_supabase_storage.py
```

This creates the `ndvi-thumbnails` bucket with proper permissions.

### 4. Run Pipeline

```bash
# Process all active lands
python main_updated.py
```

Or use GitHub Actions for automated daily runs (see `.github/workflows/ndvi-pipeline.yml`).

---

## 📊 What Gets Computed

### **Vegetation Indices**

| Index | Formula | Purpose |
|-------|---------|---------|
| **NDVI** | `(NIR - Red) / (NIR + Red)` | Overall vegetation health & biomass |
| **NDRE** | `(NIR - RedEdge) / (NIR + RedEdge)` | Nitrogen stress detection |
| **NDWI** | `(Green - NIR) / (Green + NIR)` | Water stress indicator |

### **SAR Soil Moisture**

Uses Sentinel-1 VV and VH polarizations to estimate soil moisture:

```python
# Combined method (recommended)
SM = 0.7 * VV_dB + 0.3 * (VH_dB - VV_dB)
```

Interpretation:
- `> -8 dB`: Very wet
- `-12 to -15 dB`: Moderate moisture
- `< -18 dB`: Very dry

### **Temporal Metrics**

- **NDVI Trend**: Linear regression slope (positive = growing, negative = declining)
- **Spatial Variability**: Coefficient of variation (CV) for field uniformity
- **Multi-date statistics**: Min, max, mean, standard deviation

---

## 🎯 Crop-Specific Health Classification

The pipeline uses **research-based thresholds** for different crops:

| Crop | Critical | Low | Moderate | Healthy |
|------|----------|-----|----------|---------|
| **Rice** | < 0.25 | < 0.40 | < 0.55 | ≥ 0.70 |
| **Wheat** | < 0.35 | < 0.50 | < 0.65 | ≥ 0.75 |
| **Sugarcane** | < 0.45 | < 0.60 | < 0.75 | ≥ 0.85 |
| **Cotton** | < 0.30 | < 0.45 | < 0.60 | ≥ 0.70 |
| **Maize** | < 0.35 | < 0.50 | < 0.70 | ≥ 0.80 |

*Default thresholds are used for unspecified crops.*

---

## 📁 Output Structure

### **Database (Supabase)**

#### `ndvi_data` table (time-series)
```json
{
  "land_id": "uuid",
  "tenant_id": "uuid",
  "date": "2025-12-27",
  "ndvi_value": 0.723,
  "min_ndvi": 0.456,
  "max_ndvi": 0.891,
  "ndwi_value": 0.123,
  "soil_moisture": -12.45,
  "image_url": "https://...",
  "metadata": {
    "ndvi_trend": 0.0234,
    "health_label": "Healthy",
    "alerts": [],
    "valid_observations": 8
  }
}
```

#### `lands` table (snapshot)
Updated fields:
- `last_ndvi_value`
- `last_ndvi_calculation`
- `ndvi_thumbnail_url`
- `ndvi_status`

### **Storage (Supabase Storage)**

```
ndvi-thumbnails/
├── {tenant_id}/
│   ├── {land_id}/
│   │   ├── 2025-12-27_ndvi.png
│   │   ├── 2025-12-27_ndvi_metadata.json
│   │   ├── 2025-12-26_ndvi.png
│   │   └── ...
```

**Thumbnail features:**
- 512x512 pixels (configurable)
- Adaptive contrast stretching (2nd-98th percentile)
- Agronomic color ramp (brown → yellow → green)
- Geographic metadata (bounds, CRS, resolution)

---

## 🔧 Configuration

### `config.py`

```python
# Time window for satellite data
LOOKBACK_DAYS = 15

# Quality thresholds
MIN_VALID_PIXELS = 4          # Minimum valid pixels per scene
MAX_CLOUD_PERCENT = 30        # Maximum cloud cover

# Scene classification
VALID_SCL = [4, 5, 6]         # Vegetation, Bare soil, Water
```

### Custom Crop Thresholds

Edit `analysis_improved.py`:

```python
CROP_NDVI_THRESHOLDS["your_crop"] = {
    "critical": 0.30,
    "low": 0.45,
    "moderate": 0.60,
    "healthy": 0.70,
}
```

---

## 🆕 Recent Improvements

### ✅ **Supabase Storage Integration**

**Old**: Thumbnails saved locally to `thumbnails/ndvi/`  
**New**: Uploaded to Supabase Storage bucket with CDN URLs

**Benefits**:
- Persistent storage (no data loss on redeployment)
- Public CDN access for fast loading
- Organized by tenant/land/date
- Metadata JSON for GIS integration

### ✅ **Enhanced Soil Moisture Calculation**

**Old**: Custom formula `0.6*VV + 0.4*(VV-VH)` (non-standard)  
**New**: Research-based methods:
- **Combined** (recommended): `0.7*VV_dB + 0.3*(VH_dB - VV_dB)`
- **Cross-ratio**: `VH_dB - VV_dB`
- **VV-only**: Simple `VV_dB`

### ✅ **Crop-Specific Thresholds**

**Old**: Generic thresholds (NDVI < 0.30 = critical)  
**New**: Crop-specific ranges based on agronomic research

### ✅ **Adaptive Thumbnail Contrast**

**Old**: Fixed vmin=-0.2, vmax=0.9  
**New**: Percentile-based stretching (2nd-98th percentile) for better visualization

### ✅ **Spatial Statistics**

**Added**:
- Coefficient of Variation (CV) for field uniformity
- Percentiles (10th, 90th) to identify stressed zones
- Better detection of spatial variability issues

### ✅ **Improved Logging & Error Handling**

- Detailed processing logs for each land
- Success/failure statistics
- Non-blocking thumbnail upload errors

---

## 🔬 Scientific Validation

### **NDVI Formula Validation** ✅

```python
NDVI = (NIR - Red) / (NIR + Red)
     = (B08 - B04) / (B08 + B04)
```

**Status**: ✅ Correct (standard formula)

### **Cloud Masking** ⚠️

Current: `VALID_SCL = [4, 5, 6, 7]`

**Recommendation**: Remove SCL=7 (Unclassified)
```python
VALID_SCL = [4, 5, 6]  # Vegetation, Bare soil, Water
```

### **Temporal Validation** ⚠️

Current: Minimum 2 observations  
**Recommendation**: Increase to 3-4 for reliable trends

```python
MIN_TEMPORAL_OBSERVATIONS = 3
```

### **NDVI Range** ⚠️

Current: Clipping at `vmax=0.9`  
**Issue**: Dense crops can reach 0.85-0.95

**Fixed in new version**: `vmax=1.0`

---

## 📈 Agronomic Interpretation Guide

### **NDVI Values**

| Range | Interpretation |
|-------|----------------|
| < 0.2 | Bare soil, water, or dead vegetation |
| 0.2 - 0.3 | Very sparse vegetation, stressed crops |
| 0.3 - 0.5 | Moderate vegetation (early growth or stress) |
| 0.5 - 0.7 | Good vegetation (healthy crops) |
| 0.7 - 0.9 | Excellent vegetation (peak biomass) |
| > 0.9 | Very dense vegetation (rare, forest-level) |

### **NDVI Trend**

| Trend | Interpretation | Action |
|-------|----------------|--------|
| > +0.02 | Rapid growth | Monitor for optimal harvest timing |
| 0 to +0.02 | Stable growth | Normal development |
| 0 to -0.01 | Stable/slight decline | Monitor closely |
| -0.01 to -0.02 | Declining | Check for stress causes |
| < -0.02 | Rapid decline | Immediate investigation needed |

### **Common Alert Scenarios**

1. **"Vegetation growth declining"** + **"Low soil moisture"**  
   → **Action**: Schedule irrigation

2. **"Possible nitrogen deficiency"** + **Normal NDVI**  
   → **Action**: Soil test + fertigation planning

3. **"High field variability"** + **Moderate NDVI**  
   → **Action**: Zone-specific management, check for disease

4. **"Severe water stress"** + **Declining trend**  
   → **Action**: Emergency irrigation + check irrigation system

---

## 🛠️ Migration Guide

### **Step 1: Update Code Files**

Replace these files:
- `main.py` → `main_updated.py`
- `processor.py` → `processor_updated.py`
- `ndvi_thumbnail.py` → `ndvi_thumbnail_supabase.py`
- `analysis.py` → `analysis_improved.py`
- `sar_soil_moisture.py` → `sar_soil_moisture_improved.py`

### **Step 2: Setup Supabase Storage**

```bash
python setup_supabase_storage.py
```

### **Step 3: Update Config (Optional)**

```python
# config.py
MIN_VALID_PIXELS = 4  # Keep or increase
VALID_SCL = [4, 5, 6]  # Remove SCL=7
```

### **Step 4: Test**

```bash
# Dry run on single land
python main_updated.py
```

### **Step 5: Deploy**

Update GitHub Actions workflow if needed.

---

## 🐛 Troubleshooting

### **"No Sentinel-2 data found"**

- Check if land geometry is valid (non-zero area)
- Verify `LOOKBACK_DAYS` is sufficient for your region
- Check cloud cover threshold (`MAX_CLOUD_PERCENT`)

### **"Thumbnail upload failed"**

- Verify Supabase credentials in `.env`
- Check storage bucket exists (`setup_supabase_storage.py`)
- Falls back to local storage automatically

### **"Insufficient NDVI observations"**

- Increase `LOOKBACK_DAYS` (try 30 days)
- Lower `MIN_VALID_PIXELS` threshold
- Check if region has high cloud cover

### **"Invalid geometry"**

- Ensure `boundary_polygon_old` is valid GeoJSON
- Check coordinate system (should be EPSG:4326)

---

## 📚 References

### **NDVI Research**
- Tucker (1979): Red and photographic infrared linear combinations
- Rouse et al. (1974): Monitoring vegetation systems

### **Sentinel-2 Data**
- ESA Sentinel-2 User Handbook
- Planetary Computer STAC API

### **SAR Soil Moisture**
- Bauer-Marschallinger et al. (2018): Copernicus Global Land Service
- El Hajj et al. (2017): Sentinel-1 soil moisture retrieval

### **Crop Health**
- Daughtry et al. (2000): Red edge for crop stress
- Gao (1996): NDWI for plant water status

---

## 📧 Support

For issues or questions:
1. Check this README and code comments
2. Review Supabase logs for database/storage errors
3. Check Python logs for processing errors

---

## 📜 License

This project processes public satellite data from:
- Copernicus Sentinel-2 (ESA)
- Copernicus Sentinel-1 (ESA)
- Microsoft Planetary Computer

Data access subject to Copernicus Terms and Conditions.

---

## 🎯 Next Steps

### **Recommended Enhancements**

1. **Historical Baseline**: Store multi-year NDVI to calculate VCI (Vegetation Condition Index)
2. **Growth Stage Detection**: Phenology models for crop-specific stages
3. **Weather Integration**: Combine with rainfall/temperature data
4. **Yield Prediction**: ML models trained on NDVI + yield data
5. **Pest/Disease Detection**: Anomaly detection in spatial patterns
6. **Mobile App**: Real-time alerts and field navigation

---

**Version**: 2.0  
**Last Updated**: December 2025  
**Status**: Production-ready with Supabase Storage integration ✅
