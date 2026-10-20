"""
clear_quantity_cache.py
========================
Clears ALL quantity tracking data from the database.

WHY: The old tracker stored incorrect installed_quantity values
(daily deltas from the 'quantity' field instead of cumulative
'toDateQuantity'). This bad data must be cleared before the
fixed tracker can repopulate with correct values.

WHAT IT DOES:
  1. Deletes all rows from quantity_tracking_results
  2. Deletes all rows from quantity_tracking_cache
  3. Optionally deletes quantity-related cron job records

AFTER RUNNING:
  - Restart the server (uvicorn)
  - The background scheduler will re-fetch data within 2 minutes (startup sync)
  - Or wait for the next 8-hour sync cycle
  - Or manually trigger: python -m scheduler.unified_cron_job

Usage:
  python clear_quantity_cache.py
  python clear_quantity_cache.py --dry-run        # Show what would be deleted
  python clear_quantity_cache.py --include-cron    # Also clear cron job records
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Ensure repo root is on path
REPO_ROOT = Path(__file__).resolve().parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from dotenv import load_dotenv
load_dotenv(REPO_ROOT / ".env")


def main():
    parser = argparse.ArgumentParser(description="Clear quantity tracking cache from database")
    parser.add_argument("--dry-run", action="store_true", help="Show counts without deleting")
    parser.add_argument("--include-cron", action="store_true", help="Also clear quantity cron job records")
    args = parser.parse_args()

    from api.database import (
        SessionLocal,
        QuantityTrackingCache,
        QuantityTrackingResult,
        CronJobExecution,
    )

    db = SessionLocal()

    try:
        # Count existing records
        cache_count = db.query(QuantityTrackingCache).count()
        result_count = db.query(QuantityTrackingResult).count()

        # Get date range
        oldest = db.query(QuantityTrackingCache).order_by(
            QuantityTrackingCache.tracking_date.asc()
        ).first()
        newest = db.query(QuantityTrackingCache).order_by(
            QuantityTrackingCache.tracking_date.desc()
        ).first()

        oldest_date = oldest.tracking_date if oldest else "N/A"
        newest_date = newest.tracking_date if newest else "N/A"

        print("=" * 60)
        print("QUANTITY CACHE CLEAR")
        print("=" * 60)
        print(f"  Cache entries:  {cache_count}")
        print(f"  Detail records: {result_count}")
        print(f"  Date range:     {oldest_date} → {newest_date}")

        cron_count = 0
        if args.include_cron:
            cron_count = db.query(CronJobExecution).filter(
                CronJobExecution.job_type == "quantity"
            ).count()
            print(f"  Cron records:   {cron_count}")

        print()

        if args.dry_run:
            print("  DRY RUN — no data deleted")
            print("  Run without --dry-run to delete")
            db.close()
            return

        if cache_count == 0 and result_count == 0:
            print("  Nothing to clear — cache is already empty")
            db.close()
            return

        # Confirm
        confirm = input(f"  Delete {cache_count} cache + {result_count} result rows? [y/N]: ")
        if confirm.lower() != "y":
            print("  Cancelled")
            db.close()
            return

        # Delete
        deleted_results = db.query(QuantityTrackingResult).delete()
        deleted_cache = db.query(QuantityTrackingCache).delete()

        deleted_cron = 0
        if args.include_cron:
            deleted_cron = db.query(CronJobExecution).filter(
                CronJobExecution.job_type == "quantity"
            ).delete()

        db.commit()

        print()
        print(f"  ✓ Deleted {deleted_cache} cache entries")
        print(f"  ✓ Deleted {deleted_results} detail records")
        if args.include_cron:
            print(f"  ✓ Deleted {deleted_cron} cron job records")

        print()
        print("  NEXT STEPS:")
        print("  1. Make sure tracker.py and quantity_routes.py are updated")
        print("  2. Restart the server (uvicorn)")
        print("  3. Wait 2 minutes for startup sync, or run:")
        print("     python -m scheduler.unified_cron_job")
        print("  4. Run verify_quantity_fix.py to confirm correct values")

    except Exception as exc:
        db.rollback()
        print(f"  ✗ Error: {exc}")
        import traceback
        traceback.print_exc()
    finally:
        db.close()


if __name__ == "__main__":
    main()