"""Apply reviewed production-intelligence wiring fail-closed."""
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
def replace_once(path,old,new):
 p=ROOT/path; text=p.read_text(encoding='utf-8'); count=text.count(old)
 if count!=1: raise RuntimeError(f'{path}: expected one replacement, found {count}')
 p.write_text(text.replace(old,new),encoding='utf-8')
replace_once('indices.py','"NDRE":  ("B08", "B05"),','"NDRE":  ("B8A", "B05"),')
replace_once('indices.py','B05 RedEdge1 705nm | B08 NIR 842nm | B11 SWIR1 1610nm','B05 RedEdge1 705nm | B8A RedEdge4 865nm | B08 NIR 842nm | B11 SWIR1 1610nm')
replace_once('indices.py','out["NDRE"] = _nd(b["B08"], b["B05"])','out["NDRE"] = _nd(b["B8A"], b["B05"])')
replace_once('indices.py','Keys B02 B03 B04 B05 B08 B11 (subset permitted).','Keys B02 B03 B04 B05 B8A B08 B11 (subset permitted).')
replace_once('processor.py','S2_BANDS_20M = ["B05", "B11"]','S2_BANDS_20M = ["B05", "B8A", "B11"]')
marker='        row["processing_duration_ms"] = int((time.time() - _t0) * 1000)'
insert='''        row["processing_duration_ms"] = int((time.time() - _t0) * 1000)\n\n        from production_intelligence import calibration_provenance\n        row.setdefault("metadata", {})\n        row["metadata"]["calibration"] = {bk: calibration_provenance(item, bk) for bk in ("B02","B03","B04","B05","B8A","B08","B11") if bk in item.assets}'''
replace_once('processor.py',marker,insert)
replace_once('processor.py','        evidence = {\n','        from production_intelligence import spatial_effective_sample_size\n        spatial_support = spatial_effective_sample_size(idx.get("NDVI"), crop_w)\n\n        evidence = {\n')
replace_once('processor.py','            "n_eff_kish": ndvi_stats["n_eff"],\n            "ndvi_spatial_se": ndvi_stats["se"],','            "n_eff_kish": ndvi_stats["n_eff"],\n            "n_eff_spatial": spatial_support.get("n_eff_spatial"),\n            "spatial_se": spatial_support.get("spatial_se"),\n            "spatial_support_method": spatial_support.get("method"),\n            "ndvi_spatial_se": spatial_support.get("spatial_se") or ndvi_stats["se"],')
replace_once('main.py','from pathlib import Path','from pathlib import Path')
# Proactive evaluator must consume the governed decision-grade view, never raw mixed-source ndvi_data.
replace_once('supabase/functions/proactive-evaluator/index.ts',"supabase.from('ndvi_data')\n      .select('land_id, ndvi_value, date')\n      .in('land_id', landIds)\n      .order('date', { ascending: false })\n      .limit(1000),","supabase.from('v_ndvi_decision_grade')\n      .select('land_id, ndvi_value, acquisition_date, is_fresh, observation_source, quality_score, effective_pixel_count')\n      .in('land_id', landIds)\n      .eq('observation_source', 'sentinel-2')\n      .order('acquisition_date', { ascending: false })\n      .limit(1000),")
print('Production intelligence surgical wiring applied successfully.')
