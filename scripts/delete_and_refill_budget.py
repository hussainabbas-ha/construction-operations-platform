"""
delete_and_refill_budget.py
──────────────────────────────────────────────────────────────────────────────
Phase 1 — DELETE all budget data from DB for June 8 → today
Phase 2 — REFILL using the EXACT same function as unified_cron_job.py
           (process_budget_for_date — zero difference from production cron)

Usage:
  python scripts/delete_and_refill_budget.py
  python scripts/delete_and_refill_budget.py --start 2026-06-08 --end 2026-06-15
  python scripts/delete_and_refill_budget.py --dry-run
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
logging.getLogger("budget_tracking.hcss_client").setLevel(logging.WARNING)
logging.getLogger("payroll_verification.hcss_client").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

from api.database import (
    SessionLocal, create_tables,
    BudgetTrackingResult, BudgetTrackingCache,
)
# ── EXACT same function the production cron job calls ────────────────────────
from scheduler.unified_cron_job import process_budget_for_date


def build_date_list(start: date, end: date) -> list[str]:
    dates, d = [], start
    while d <= end:
        dates.append(d.isoformat())
        d += timedelta(days=1)
    return dates


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Delete budget DB data for a date range, then refill using "
            "the exact same logic as the unified cron job."
        )
    )
    parser.add_argument(
        "--start",
        default="2026-06-08",
        help="Start date YYYY-MM-DD (default: 2026-06-08)",
    )
    parser.add_argument(
        "--end",
        default=date.today().isoformat(),
        help="End date YYYY-MM-DD (default: today)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview what will happen without touching the DB or calling HCSS",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=1.0,
        help="Seconds between HCSS calls per date (default: 1.0)",
    )
    args = parser.parse_args()

    start_date = date.fromisoformat(args.start)
    end_date   = date.fromisoformat(args.end)

    if end_date < start_date:
        print("ERROR: --end must be on or after --start")
        sys.exit(1)

    dates = build_date_list(start_date, end_date)

    create_tables()
    db = SessionLocal()

    try:
        # ── Preview: count existing rows per date ──────────────────────────
        print()
        print("=" * 70)
        print("  BUDGET — DELETE + REFILL")
        print("=" * 70)
        print(f"  Start date   : {args.start}")
        print(f"  End date     : {args.end}")
        print(f"  Total days   : {len(dates)}")
        print(f"  Dry run      : {'YES — nothing will change' if args.dry_run else 'no'}")
        print(f"  Cron function: process_budget_for_date (unified_cron_job.py)")
        print()

        print("  Current DB state for this range:")
        total_existing = 0
        for d in dates:
            n = (db.query(BudgetTrackingResult)
                 .filter(BudgetTrackingResult.tracking_date == d)
                 .count())
            total_existing += n
            flag = f"  ← {n} rows will be DELETED" if n > 0 else "  (no data)"
            print(f"    {d}{flag}")

        print()
        print(f"  Total rows to delete : {total_existing}")
        print("=" * 70)
        print()

        if args.dry_run:
            print("  DRY RUN — no changes made. Remove --dry-run to execute.")
            print()
            return

        confirm = input(
            "  This will DELETE all budget rows for the range above\n"
            "  and immediately refill from HCSS.\n"
            "  Proceed? [y/N] "
        ).strip().lower()
        if confirm != "y":
            print("  Aborted.")
            sys.exit(0)
        print()

        # ══════════════════════════════════════════════════════════════════
        # PHASE 1 — DELETE
        # ══════════════════════════════════════════════════════════════════
        print("  ── PHASE 1: DELETE ──────────────────────────────────────────")
        result_deleted = 0
        cache_deleted  = 0
        for d in dates:
            r = (db.query(BudgetTrackingResult)
                 .filter(BudgetTrackingResult.tracking_date == d)
                 .delete())
            c = (db.query(BudgetTrackingCache)
                 .filter(BudgetTrackingCache.tracking_date == d)
                 .delete())
            result_deleted += r
            cache_deleted  += c

        db.commit()
        print(f"  Deleted {result_deleted} result rows + {cache_deleted} cache rows")
        print()

        # ══════════════════════════════════════════════════════════════════
        # PHASE 2 — REFILL (exact cron job logic)
        # ══════════════════════════════════════════════════════════════════
        print("  ── PHASE 2: REFILL (process_budget_for_date) ───────────────")
        print()

        stored        = 0
        empty         = 0
        errors        = 0
        total_records = 0
        start_ts      = time.perf_counter()

        for i, target_date in enumerate(dates, 1):
            print(f"  [{i:>2}/{len(dates)}] {target_date}", end=" ... ", flush=True)

            # ── EXACT same call as the production cron job ────────────────
            success, records, error = process_budget_for_date(db, target_date)

            if success and records > 0:
                stored        += 1
                total_records += records
                print(f"{records:>4} cost codes stored")
            elif success and records == 0:
                empty += 1
                print("0 cost codes  (no activity this date)")
            else:
                errors += 1
                print(f"ERROR — {error}")

            if i < len(dates):
                time.sleep(args.delay)

    except KeyboardInterrupt:
        print()
        print("  Interrupted — some dates may not have been refilled.")
        print("  Re-run the script. Completed dates will be cleanly overwritten.")
    finally:
        db.close()

    elapsed = time.perf_counter() - start_ts

    print()
    print("=" * 70)
    print("  COMPLETE")
    print("=" * 70)
    print(f"  Elapsed        : {elapsed / 60:.1f} min")
    print(f"  Dates stored   : {stored}  ({total_records} total cost codes)")
    print(f"  Dates empty    : {empty}  (no HCSS activity)")
    print(f"  Errors         : {errors}")
    if errors:
        print()
        print("  Re-run to retry failed dates.")
    print("=" * 70)
    print()


if __name__ == "__main__":
    main()
