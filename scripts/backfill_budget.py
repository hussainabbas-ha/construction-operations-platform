"""
backfill_budget.py
──────────────────────────────────────────────────────────────────────────────
Backfill budget data into the DB for a given date range.

Uses the same logic as unified_cron_job.process_budget_for_date — stores
budgeted_all_cost (raw HCSS total) in DB. expected_budget is NOT stored;
it is computed on the fly at response time using:
    expected_budget = (quantity / planned_quantity) * budgeted_all_cost

USAGE:
  python scripts/backfill_budget.py --start 2026-01-01 --end 2026-06-15
  python scripts/backfill_budget.py --start 2026-05-01 --end 2026-05-31 --dry-run
  python scripts/backfill_budget.py --start 2026-06-01 --end 2026-06-15 --delay 2
  python scripts/backfill_budget.py --start 2026-01-01 --end 2026-06-15 --overwrite

OPTIONS:
  --start     Start date YYYY-MM-DD  (default: 2026-01-01)
  --end       End date   YYYY-MM-DD  (default: today)
  --dry-run   Fetch from HCSS but do NOT write to DB
  --delay     Seconds between per-date HCSS calls (default: 1.0)
  --overwrite Re-fetch and overwrite dates that already have data
              (default: skip dates that already have data)
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)-8s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

from api.database import SessionLocal, create_tables, BudgetTrackingCache
from scheduler.unified_cron_job import process_budget_for_date


# ── Helpers ───────────────────────────────────────────────────────────────────

def build_date_list(start: date, end: date) -> list[str]:
    dates, d = [], start
    while d <= end:
        dates.append(d.isoformat())
        d += timedelta(days=1)
    return dates


def already_has_data(db, target_date: str) -> bool:
    """Return True if the DB already has budget rows for this date."""
    row = db.query(BudgetTrackingCache).filter(
        BudgetTrackingCache.tracking_date == target_date
    ).first()
    return row is not None and (row.total_cost_codes or 0) > 0


def get_existing_count(db, target_date: str) -> int:
    """Return how many cost codes are stored for this date (0 if none)."""
    row = db.query(BudgetTrackingCache).filter(
        BudgetTrackingCache.tracking_date == target_date
    ).first()
    return row.total_cost_codes if row else 0


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Backfill budget DB from HCSS for a date range. "
            "Stores budgeted_all_cost (raw HCSS total). "
            "expected_budget is computed on the fly at response time."
        )
    )
    parser.add_argument("--start",     default="2026-01-01",
                        help="Start date YYYY-MM-DD (default: 2026-01-01)")
    parser.add_argument("--end",       default=date.today().isoformat(),
                        help="End date YYYY-MM-DD (default: today)")
    parser.add_argument("--dry-run",   action="store_true",
                        help="Fetch from HCSS but do NOT write to DB")
    parser.add_argument("--overwrite", action="store_true",
                        help="Re-fetch and overwrite dates that already have data")
    parser.add_argument("--delay",     type=float, default=1.0,
                        help="Seconds between HCSS calls (default: 1.0)")
    args = parser.parse_args()

    start_date = date.fromisoformat(args.start)
    end_date   = date.fromisoformat(args.end)
    dates      = build_date_list(start_date, end_date)

    print("=" * 70)
    print("  BUDGET BACKFILL")
    print("  Stores: budgeted_all_cost (raw HCSS total)")
    print("  expected_budget computed on the fly at response time from quantity data")
    print("=" * 70)
    print(f"  Start      : {args.start}")
    print(f"  End        : {args.end}")
    print(f"  Total days : {len(dates)}")
    print(f"  Dry run    : {args.dry_run}")
    print(f"  Overwrite  : {args.overwrite}")
    print(f"  Delay      : {args.delay}s per date")
    print(f"  Est. time  : ~{len(dates) * args.delay / 60:.1f} min")
    print("=" * 70)
    print()

    create_tables()
    db = SessionLocal()

    stored = skipped = empty = errors = total_records = 0

    try:
        for i, target_date in enumerate(dates, 1):
            prefix = f"  [{i:>3}/{len(dates)}] {target_date}"

            # Skip if already populated and --overwrite not set
            if not args.overwrite and already_has_data(db, target_date):
                existing = get_existing_count(db, target_date)
                skipped += 1
                print(f"{prefix}  (skipped — {existing} records already in DB)")
                continue

            print(f"{prefix}", end=" ... ", flush=True)

            if args.dry_run:
                # Dry run: do the HCSS fetch but roll back — nothing persisted
                dry_db = SessionLocal()
                try:
                    success, count, error = process_budget_for_date(dry_db, target_date)
                    dry_db.rollback()
                finally:
                    dry_db.close()
            else:
                success, count, error = process_budget_for_date(db, target_date)

            if success and count > 0:
                stored += 1
                total_records += count
                tag = "(dry run — not saved)" if args.dry_run else "stored"
                print(f"{count:>4} cost codes {tag}")
            elif success and count == 0:
                empty += 1
                print("0 records (no timecard activity this date)")
            else:
                errors += 1
                print(f"ERROR — {error}")
                logger.error("Failed %s: %s", target_date, error)

            if i < len(dates):
                time.sleep(args.delay)

    finally:
        db.close()

    print()
    print("=" * 70)
    print("  DONE")
    print(f"  Stored     : {stored} dates  ({total_records} total cost-code records)")
    print(f"  Skipped    : {skipped} dates  (already in DB — use --overwrite to redo)")
    print(f"  Empty      : {empty} dates  (no timecard activity)")
    print(f"  Errors     : {errors} dates")
    if args.dry_run:
        print()
        print("  DRY RUN — nothing was written to the DB.")
        print("  Re-run without --dry-run to populate.")
    print("=" * 70)


if __name__ == "__main__":
    main()
