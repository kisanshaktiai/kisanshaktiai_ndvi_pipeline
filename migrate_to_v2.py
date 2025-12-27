"""
migrate_to_v2.py
---------------

Migration helper script to upgrade from v1 to v2 of NDVI pipeline.

This script:
1. Backs up current code
2. Validates environment
3. Checks Supabase connection
4. Verifies storage setup
5. Provides migration checklist

Run before deploying v2 code.
"""

import os
import sys
import shutil
from datetime import datetime
from pathlib import Path

try:
    from dotenv import load_dotenv
    from supabase import create_client
except ImportError:
    print("❌ Missing dependencies. Run: pip install -r requirements_updated.txt")
    sys.exit(1)


def print_header(text):
    print(f"\n{'='*60}")
    print(f"  {text}")
    print(f"{'='*60}\n")


def backup_files():
    """Backup current code before migration."""
    print_header("STEP 1: Backing Up Current Code")
    
    backup_dir = Path(f"backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}")
    backup_dir.mkdir(exist_ok=True)
    
    files_to_backup = [
        "main.py",
        "processor.py",
        "ndvi_thumbnail.py",
        "analysis.py",
        "sar_soil_moisture.py",
        "config.py",
        "requirements.txt",
    ]
    
    for file in files_to_backup:
        if Path(file).exists():
            shutil.copy2(file, backup_dir / file)
            print(f"✓ Backed up: {file}")
    
    print(f"\n✅ Backup complete: {backup_dir}/")
    return backup_dir


def validate_environment():
    """Check environment setup."""
    print_header("STEP 2: Validating Environment")
    
    load_dotenv()
    
    required_vars = ["SUPABASE_URL", "SUPABASE_KEY"]
    missing = []
    
    for var in required_vars:
        if os.getenv(var):
            print(f"✓ {var} found")
        else:
            print(f"✗ {var} missing")
            missing.append(var)
    
    if missing:
        print(f"\n❌ Missing environment variables: {', '.join(missing)}")
        print("   Add them to .env file")
        return False
    
    print("\n✅ Environment variables configured")
    return True


def test_supabase_connection():
    """Test Supabase database connection."""
    print_header("STEP 3: Testing Supabase Connection")
    
    try:
        load_dotenv()
        supabase = create_client(
            os.getenv("SUPABASE_URL"),
            os.getenv("SUPABASE_KEY")
        )
        
        # Test query
        result = supabase.table("lands").select("id").limit(1).execute()
        
        print("✓ Database connection successful")
        print(f"✓ Found {len(result.data)} test record(s)")
        print("\n✅ Supabase connection OK")
        return True, supabase
        
    except Exception as e:
        print(f"\n❌ Supabase connection failed: {e}")
        return False, None


def check_storage_bucket(supabase):
    """Verify storage bucket exists."""
    print_header("STEP 4: Checking Storage Bucket")
    
    try:
        buckets = supabase.storage.list_buckets()
        bucket_names = [b["name"] for b in buckets]
        
        if "ndvi-thumbnails" in bucket_names:
            print("✓ Bucket 'ndvi-thumbnails' exists")
            print("\n✅ Storage bucket ready")
            return True
        else:
            print("✗ Bucket 'ndvi-thumbnails' not found")
            print("\nAvailable buckets:", bucket_names)
            print("\n⚠️  Run: python setup_supabase_storage.py")
            return False
            
    except Exception as e:
        print(f"\n❌ Storage check failed: {e}")
        print("   May need to run: python setup_supabase_storage.py")
        return False


def migration_checklist():
    """Display migration checklist."""
    print_header("STEP 5: Migration Checklist")
    
    checklist = [
        ("Backup current code", "Automatic"),
        ("Update environment variables", "Check .env"),
        ("Install new dependencies", "pip install -r requirements_updated.txt"),
        ("Setup Supabase Storage", "python setup_supabase_storage.py"),
        ("Replace code files", "Copy *_updated.py → *.py"),
        ("Update config.py", "See TECHNICAL_ANALYSIS.md"),
        ("Test on single land", "python main_updated.py (dry run)"),
        ("Deploy to production", "Update GitHub Actions workflow"),
        ("Monitor first run", "Check logs and database"),
    ]
    
    for idx, (task, action) in enumerate(checklist, 1):
        print(f"{idx}. [ ] {task}")
        print(f"      → {action}\n")
    
    print("📚 See README_UPDATED.md for detailed instructions")


def show_file_mappings():
    """Show which files to replace."""
    print_header("FILE REPLACEMENT MAP")
    
    mappings = [
        ("main.py", "main_updated.py", "Main pipeline entry point"),
        ("processor.py", "processor_updated.py", "Land processing logic"),
        ("ndvi_thumbnail.py", "ndvi_thumbnail_supabase.py", "Thumbnail with storage"),
        ("analysis.py", "analysis_improved.py", "Crop-specific thresholds"),
        ("sar_soil_moisture.py", "sar_soil_moisture_improved.py", "Corrected SAR methods"),
        ("requirements.txt", "requirements_updated.txt", "Updated dependencies"),
    ]
    
    print("OLD FILE → NEW FILE (Purpose)")
    print("-" * 60)
    for old, new, desc in mappings:
        print(f"{old:25} → {new:30}")
        print(f"{' '*25}   ({desc})\n")
    
    print("\n⚠️  RECOMMENDATION: Keep old files as *.old backup")
    print("   Example: mv main.py main.py.old && cp main_updated.py main.py")


def main():
    """Run migration validation."""
    
    print("\n" + "="*60)
    print("  NDVI PIPELINE v1 → v2 MIGRATION HELPER")
    print("="*60)
    
    # Step 1: Backup
    backup_dir = backup_files()
    
    # Step 2: Environment
    if not validate_environment():
        print("\n❌ Fix environment issues before proceeding")
        return 1
    
    # Step 3: Database
    success, supabase = test_supabase_connection()
    if not success:
        print("\n❌ Fix Supabase connection before proceeding")
        return 1
    
    # Step 4: Storage
    storage_ok = check_storage_bucket(supabase)
    if not storage_ok:
        print("\n⚠️  Storage needs setup (not critical for testing)")
    
    # Step 5: Checklist
    migration_checklist()
    
    # Step 6: File mappings
    show_file_mappings()
    
    # Final summary
    print_header("MIGRATION SUMMARY")
    print(f"✅ Backup created: {backup_dir}/")
    print(f"✅ Environment: OK")
    print(f"✅ Database: Connected")
    print(f"{'✅' if storage_ok else '⚠️ '} Storage: {'Ready' if storage_ok else 'Needs setup'}")
    
    print("\n📋 NEXT STEPS:")
    print("1. Review TECHNICAL_ANALYSIS.md for detailed changes")
    print("2. Run: pip install -r requirements_updated.txt")
    if not storage_ok:
        print("3. Run: python setup_supabase_storage.py")
    print("4. Replace code files (see FILE REPLACEMENT MAP above)")
    print("5. Test: python main_updated.py")
    print("6. Deploy to production")
    
    print("\n✅ Ready for migration!\n")
    return 0


if __name__ == "__main__":
    exit(main())
