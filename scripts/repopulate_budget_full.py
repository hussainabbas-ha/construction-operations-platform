"""
repopulate_budget_full.py
─────────────────────────────────────────────────────────────────────────────
Backfills budget DB for every date in a given date range.

For each date:
  - Fetches budget from HCSS (date-filtered, all cost types, correct budget)
  - Stores/overwrites the DB row with all fields
  - Old incorrect rows are replaced automatically

Usage:
  python scripts/repopulate_budget_full.py --start 2026-01-01 --end 2026-06-08
  python scripts/repopulate_budget_full.py --start 2026-01-01 --end 2026-06-08 --dry-run
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

from api.database import SessionLocal, create_tables
from api.db_services import store_budget_tracking
from budget_tracking.hcss_client import BudgetHCSSClient
from budget_tracking.analyzer import calculate_budget_status


def build_date_list(start: date, end: date) -> list[str]:
    dates, d = [], start
    while d <= end:
        dates.append(d.isoformat())
        d += timedelta(days=1)
    return dates


def process_one_date(
    client: BudgetHCSSClient,
    db,
    target_date: str,
    dry_run: bool,
) -> tuple[int, str]:
    """Returns (record_count, status) where status is 'stored' | 'empty' | 'error'."""
    try:
        raw = client.fetch_budget_summary(date=target_date)
    except Exception as exc:
        logger.error("HCSS fetch failed for %s: %s", target_date, exc)
        return 0, "error"

    if not raw:
        if not dry_run:
            store_budget_tracking(db, target_date, [])
        return 0, "empty"

    budget_results = calculate_budget_status(raw)

    # Build lookup for extended fields
    raw_by_key: dict[tuple, dict] = {}
    for item in raw:
        jid  = (item.get("job")      or {}).get("jobId",      "")
        ccid = (item.get("costCode") or {}).get("costCodeId", "")
        if jid and ccid:
            raw_by_key[(jid, ccid)] = item

    dicts = []
    for r in budget_results:
        extra = raw_by_key.get((r.job_id, r.cost_code_id), {})
        dicts.append({
            "cost_code_id":           r.cost_code_id,
            "cost_code":              r.cost_code,
            "cost_code_description":  r.cost_code_description,
            "job_id":                 r.job_id,
            "job_name":               r.job_name,
            "business_unit":          r.business_unit,
            "expected_budget":        r.expected_budget,
            "actual_cost":            r.actual_cost,
            "utilization_percentage": r.utilization_percentage,
            "variance":               r.variance,
            "status":                 r.status,
            "labor_cost":       extra.get("laborCost",           0.0),
            "equipment_cost":   extra.get("equipmentCost",       0.0),
            "material_cost":    extra.get("materialCost",        0.0),
            "subcontract_cost": extra.get("subcontractCost",     0.0),
            "trucking_cost":    extra.get("truckingCost",        0.0),
            "labor_hours":      extra.get("laborHours",          0.0),
            "quantity":         extra.get("quantity",            0.0),
            "labor_budget":       extra.get("laborBudget",       0.0),
            "equipment_budget":   extra.get("equipmentBudget",   0.0),
            "material_budget":    extra.get("materialBudget",    0.0),
            "subcontract_budget": extra.get("subcontractBudget", 0.0),
        })

    if not dry_run:
        _, count = store_budget_tracking(db, target_date, dicts)
    else:
        count = len(dicts)

    return count, "stored"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backfill budget DB from HCSS for a date range"
    )
    parser.add_argument("--start",   default="2026-01-01",
                        help="Start date YYYY-MM-DD (default: 2026-01-01)")
    parser.add_argument("--end",     default=date.today().isoformat(),
                        help="End date YYYY-MM-DD (default: today)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would happen without writing to DB")
    parser.add_argument("--delay",   type=float, default=0.5,
                        help="Seconds between HCSS calls (default: 0.5)")
    args = parser.parse_args()

    start_date = date.fromisoformat(args.start)
    end_date   = date.fromisoformat(args.end)
    dates      = build_date_list(start_date, end_date)

    print("=" * 65)
    print("  BUDGET BACKFILL")
    print("=" * 65)
    print(f"  Start      : {args.start}")
    print(f"  End        : {args.end}")
    print(f"  Total days : {len(dates)}")
    print(f"  Dry run    : {args.dry_run}")
    print(f"  Estimated  : ~{len(dates) * args.delay / 60:.1f} min at {args.delay}s/date")
    print("=" * 65)
    print()

    create_tables()
    client = BudgetHCSSClient()
    db     = SessionLocal()

    stored = empty = errors = total_records = 0

    try:
        for i, d in enumerate(dates, 1):
            print(f"  [{i:>3}/{len(dates)}] {d}", end=" ... ", flush=True)
            count, status = process_one_date(client, db, d, args.dry_run)

            if status == "stored":
                stored += 1
                total_records += count
                suffix = "(dry run)" if args.dry_run else "stored"
                print(f"{count:>4} records {suffix}")
            elif status == "empty":
                empty += 1
                print("0 records (no activity)")
            else:
                errors += 1
                print("ERROR — see log above")

            if i < len(dates):
                time.sleep(args.delay)

    finally:
        db.close()

    print()
    print("=" * 65)
    print(f"  DONE")
    print(f"  Dates with data    : {stored}  ({total_records} records)")
    print(f"  Dates with no data : {empty}  (weekends / holidays)")
    print(f"  Errors             : {errors}")
    if args.dry_run:
        print()
        print("  This was a DRY RUN — nothing was written.")
        print("  Run without --dry-run to populate the DB.")
    print("=" * 65)


if __name__ == "__main__":
    main()
