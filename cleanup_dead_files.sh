#!/usr/bin/env bash
# =====================================================================
# cleanup_dead_files.sh  -  remove the v1/v2 tree that no longer runs
#
# Generated 2026-08-30 from the ACTUAL file list of origin/main at
# commit 6fba353. Every path below was verified to exist on origin, and
# every path NOT listed was verified to be part of the running system.
#
# WHAT RUNS TODAY (kept, never touched):
#   main.py processor.py raster_utils.py indices.py quality.py
#   config.py db.py sar_vegetation.py tile_grouping.py phenology.py
#   sentinel_search.py logger.py
#   requirements.txt requirements-dev.txt README.md .gitattributes
#   .github/workflows/ndvi-pipeline.yml  .github/workflows/tests.yml
#   tests/  sql/
#
# The 84 files removed here are dead: the *_fixed / *_updated /
# *_improved modules fail at import (they call functions that no longer
# exist), the thumbnail/geotiff/storage modules are not reached by any
# runtime path, the .pyc files are build output, rasters/ and
# thumbnails/ are December-2025 artifacts the pipeline no longer writes,
# and the two .sql files use "CREATE POLICY IF NOT EXISTS", which is not
# valid PostgreSQL and has never been runnable.
#
# The stale .md files actively mislead: README_UPDATED.md still tells a
# reader to run "python main_updated.py", a module that crashes on
# import. README.md is the accurate one and is kept.
#
# USAGE (from the repo root, on a clean working tree):
#     bash cleanup_dead_files.sh      # stage the deletions
#     git status                      # review
#     git commit -m "remove dead v1/v2 tree; add .gitignore"
#     git push
#
# Nothing is removed from the database, from Supabase Storage, or from
# git history - only from the working tree and the index. Every deleted
# file stays recoverable from earlier commits.
# =====================================================================
set -euo pipefail

if [ ! -f main.py ] || [ ! -d .github/workflows ]; then
  echo "ERROR: run this from the repository root." >&2
  exit 1
fi

removed=0
rm_tracked() {   # delete only what git actually tracks
  if git ls-files --error-unmatch "$1" >/dev/null 2>&1; then
    git rm -q --cached -- "$1"
    rm -f -- "$1"
    removed=$((removed+1))
  else
    echo "  skip (not tracked): $1"
  fi
}

echo "-- Broken / dead Python modules (import-broken or never imported by main.py) (18)"
rm_tracked "analysis.py"
rm_tracked "analysis_improved.py"
rm_tracked "indices_fixed.py"
rm_tracked "main_fixed.py"
rm_tracked "main_updated.py"
rm_tracked "migrate_to_v2.py"
rm_tracked "ndvi_escalation_worker.py"
rm_tracked "ndvi_geotiff.py"
rm_tracked "ndvi_thumbnail.py"
rm_tracked "ndvi_thumbnail_supabase.py"
rm_tracked "processor_updated.py"
rm_tracked "raster_utils_fixed.py"
rm_tracked "sar_soil_moisture.py"
rm_tracked "sar_soil_moisture_improved.py"
rm_tracked "sentinel1_pc.py"
rm_tracked "sentinel2_pc.py"
rm_tracked "setup_supabase_storage.py"
rm_tracked "storage.py"

echo "-- Committed bytecode (12)"
rm_tracked "__pycache__/analysis.cpython-314.pyc"
rm_tracked "__pycache__/config.cpython-314.pyc"
rm_tracked "__pycache__/db.cpython-314.pyc"
rm_tracked "__pycache__/indices.cpython-314.pyc"
rm_tracked "__pycache__/logger.cpython-314.pyc"
rm_tracked "__pycache__/ndvi_geotiff.cpython-314.pyc"
rm_tracked "__pycache__/ndvi_thumbnail.cpython-314.pyc"
rm_tracked "__pycache__/processor.cpython-314.pyc"
rm_tracked "__pycache__/raster_utils.cpython-314.pyc"
rm_tracked "__pycache__/sar_soil_moisture.cpython-314.pyc"
rm_tracked "__pycache__/sentinel1_pc.cpython-314.pyc"
rm_tracked "__pycache__/sentinel2_pc.cpython-314.pyc"

echo "-- Committed output artifacts from Dec 2025 (pipeline no longer writes these) (39)"
rm_tracked "rasters/ndvi/0afb5d63-ca41-486b-ab99-ea8a311c86d0_ndvi.tif"
rm_tracked "rasters/ndvi/156a9236-f822-444a-a45d-6fcf90c294b7_ndvi.tif"
rm_tracked "rasters/ndvi/285a58e4-5caa-4a8e-95de-c4f816876784_ndvi.tif"
rm_tracked "rasters/ndvi/3307fac1-1ab2-4e91-ac90-2479ab47a0f0_ndvi.tif"
rm_tracked "rasters/ndvi/3b10c80d-dba9-4a9c-af6c-5689f314b797_ndvi.tif"
rm_tracked "rasters/ndvi/3fe4bcbe-a357-4c34-bbe0-fb9991a31605_ndvi.tif"
rm_tracked "rasters/ndvi/4af38aa2-8fa7-4482-8388-b71bd3eb6e12_ndvi.tif"
rm_tracked "rasters/ndvi/555785e5-872f-4bbf-8d97-4f7b5a36698e_ndvi.tif"
rm_tracked "rasters/ndvi/5805d831-462f-4ed2-8bc0-b2676fc7b7a1_ndvi.tif"
rm_tracked "rasters/ndvi/61eeca63-1d9d-4a9d-b661-db8d46a187b0_ndvi.tif"
rm_tracked "rasters/ndvi/ca9687fa-e0d8-41fa-b77c-07325384a898_ndvi.tif"
rm_tracked "rasters/ndvi/e2965883-dee9-4872-989d-6d4c07f52dd9_ndvi.tif"
rm_tracked "rasters/ndvi/f1a0a6ed-2463-4790-8dd3-fb28c62d4e68_ndvi.tif"
rm_tracked "thumbnails/NDVI/0afb5d63-ca41-486b-ab99-ea8a311c86d0.json"
rm_tracked "thumbnails/NDVI/0afb5d63-ca41-486b-ab99-ea8a311c86d0.png"
rm_tracked "thumbnails/NDVI/156a9236-f822-444a-a45d-6fcf90c294b7.json"
rm_tracked "thumbnails/NDVI/156a9236-f822-444a-a45d-6fcf90c294b7.png"
rm_tracked "thumbnails/NDVI/285a58e4-5caa-4a8e-95de-c4f816876784.json"
rm_tracked "thumbnails/NDVI/285a58e4-5caa-4a8e-95de-c4f816876784.png"
rm_tracked "thumbnails/NDVI/3307fac1-1ab2-4e91-ac90-2479ab47a0f0.json"
rm_tracked "thumbnails/NDVI/3307fac1-1ab2-4e91-ac90-2479ab47a0f0.png"
rm_tracked "thumbnails/NDVI/3b10c80d-dba9-4a9c-af6c-5689f314b797.json"
rm_tracked "thumbnails/NDVI/3b10c80d-dba9-4a9c-af6c-5689f314b797.png"
rm_tracked "thumbnails/NDVI/3fe4bcbe-a357-4c34-bbe0-fb9991a31605.json"
rm_tracked "thumbnails/NDVI/3fe4bcbe-a357-4c34-bbe0-fb9991a31605.png"
rm_tracked "thumbnails/NDVI/4af38aa2-8fa7-4482-8388-b71bd3eb6e12.json"
rm_tracked "thumbnails/NDVI/4af38aa2-8fa7-4482-8388-b71bd3eb6e12.png"
rm_tracked "thumbnails/NDVI/555785e5-872f-4bbf-8d97-4f7b5a36698e.json"
rm_tracked "thumbnails/NDVI/555785e5-872f-4bbf-8d97-4f7b5a36698e.png"
rm_tracked "thumbnails/NDVI/5805d831-462f-4ed2-8bc0-b2676fc7b7a1.json"
rm_tracked "thumbnails/NDVI/5805d831-462f-4ed2-8bc0-b2676fc7b7a1.png"
rm_tracked "thumbnails/NDVI/61eeca63-1d9d-4a9d-b661-db8d46a187b0.json"
rm_tracked "thumbnails/NDVI/61eeca63-1d9d-4a9d-b661-db8d46a187b0.png"
rm_tracked "thumbnails/NDVI/ca9687fa-e0d8-41fa-b77c-07325384a898.json"
rm_tracked "thumbnails/NDVI/ca9687fa-e0d8-41fa-b77c-07325384a898.png"
rm_tracked "thumbnails/NDVI/e2965883-dee9-4872-989d-6d4c07f52dd9.json"
rm_tracked "thumbnails/NDVI/e2965883-dee9-4872-989d-6d4c07f52dd9.png"
rm_tracked "thumbnails/NDVI/f1a0a6ed-2463-4790-8dd3-fb28c62d4e68.json"
rm_tracked "thumbnails/NDVI/f1a0a6ed-2463-4790-8dd3-fb28c62d4e68.png"

echo "-- Invalid SQL (CREATE POLICY IF NOT EXISTS is not valid PostgreSQL) (2)"
rm_tracked "create_ndvi_processing_logs.sql"
rm_tracked "setup_storage_buckets.sql"

echo "-- Stale docs describing code that no longer runs (11)"
rm_tracked "CRITICAL_FIX_DEPLOYMENT_GUIDE.md"
rm_tracked "FINAL_MANIFEST.md"
rm_tracked "FIX_SUMMARY.md"
rm_tracked "IMPROVEMENTS_SUMMARY.md"
rm_tracked "MCARI_FIX_COMPLETE.md"
rm_tracked "MCARI_IMPLEMENTATION.md"
rm_tracked "README_UPDATED.md"
rm_tracked "ROOT_CAUSE_ANALYSIS.md"
rm_tracked "SELF_AUDIT.md"
rm_tracked "SETUP_GUIDE.md"
rm_tracked "TECHNICAL_ANALYSIS.md"

echo "-- Other (2)"
rm_tracked "CHANGES_v3_0_1.patch"
rm_tracked "requirements_updated.txt"

# Directories left empty by the deletions above.
rmdir __pycache__ rasters/ndvi rasters thumbnails/NDVI thumbnails 2>/dev/null || true

# Stop build output and local logs re-entering the repository.
cat > .gitignore << 'GITIGNORE'
# Python build output
__pycache__/
*.py[cod]
*.egg-info/
.pytest_cache/

# Local environment
.env
.venv/
venv/

# Pipeline run output. The workflow uploads pipeline.log as a build
# artifact; it must never be committed.
pipeline.log

# Generated imagery. The pipeline writes to Supabase Storage, never to
# the repository; these directories existed only as Dec-2025 artifacts.
rasters/
thumbnails/
GITIGNORE
git add .gitignore

echo
echo "Removed $removed tracked file(s); wrote .gitignore."
echo "Expected: 84 removed, leaving 22 files + .gitignore = 23."
echo
echo "Review:  git status"
echo "Commit:  git commit -m 'remove dead v1/v2 tree; add .gitignore'"
