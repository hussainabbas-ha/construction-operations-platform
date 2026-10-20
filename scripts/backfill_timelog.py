"""
backfill_timelog.py
─────────────────────────────────────────────────────────────────────────────
Backfills time log verification data from HCSS for every calendar date in a
given range and stores the results in the DB cache.

Every day is processed — including weekends — because construction crews
can work any day of the week. If HCSS returns 0 timecards for a date, an
empty cache entry is stored so the cron job knows that date was checked.

By default, dates that already have data in the DB are skipped.
Pass --force to re-fetch and overwrite them.

Usage:
  # Preview without writing anything
  python scripts/backfill_timelog.py --dry-run

  # Backfill Jan 1 2026 → today
  python scripts/backfill_timelog.py

  # Custom range
  python scripts/backfill_timelog.py --start 2026-01-01 --end 2026-06-09

  # Re-fetch everything, overwriting existing cached data
  python scripts/backfill_timelog.py --force

  # Slower delay if HCSS returns 429 rate-limit errors
  python scripts/backfill_timelog.py --delay 2.0
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import date, timedelta
from pathlib import Path

# ── Repo root on path ─────────────────────────────────────────────────────────
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)-8s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
# Quiet noisy child loggers so the progress table stays readable
logging.getLogger("payroll_verification.verifier").setLevel(logging.WARNING)
logging.getLogger("payroll_verification.hcss_client").setLevel(logging.WARNING)
logging.getLogger("api.cache_service").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

# ── Project imports (need path above) ────────────────────────────────────────
from api.database import SessionLocal, create_tables, TimelogVerificationCache
from api.db_services import store_timelog_verification
from payroll_verification.verifier import verify_payroll_date
from sqlalchemy import and_


# =============================================================================
# HELPERS
# =============================================================================

def build_date_list(start: date, end: date) -> list[str]:
    """Every calendar date from start to end inclusive — no day excluded."""
    dates: list[str] = []
    current = start
    while current <= end:
        dates.append(current.isoformat())
        current += timedelta(days=1)
    return dates


def is_already_cached(db, target_date: str) -> bool:
    """True if the DB already has a cache entry for this date."""
    return db.query(TimelogVerificationCache).filter(
        and_(
            TimelogVerificationCache.verification_date == target_date,
            TimelogVerificationCache.business_unit_id == None,  # noqa: E711
        )
    ).first() is not None


def process_one_date(
    db,
    target_date: str,
    *,
    dry_run: bool,
    force: bool,
) -> tuple[int, str]:
    """
    Fetch, verify, and store timecards for a single date.

    Returns:
        (record_count, status)
        status: 'stored' | 'skipped' | 'empty' | 'error'
    """
    if not force and is_already_cached(db, target_date):
        return 0, "skipped"

    try:
        results = verify_payroll_date(
            target_date=target_date,
            business_unit_id=None,
        )
    except Exception as exc:
        logger.error("HCSS fetch/verify failed for %s: %s", target_date, exc)
        return 0, "error"

    if not results:
        # No timecards — write an empty cache entry so the cron job knows
        # this date was already checked and won't keep re-fetching it.
        if not dry_run:
            store_timelog_verification(db, target_date, None, [])
        return 0, "empty"

    results_dict = [
        {
            "id":              r.id,
            "date":            r.date,
            "job":             r.job_code,
            "job_id":          r.job_id,
            "job_description": r.job_description,
            "foreman":         r.foreman,
            "foreman_id":      r.foreman_id,
            "status":          r.status,
            "reasons":         r.reasons,
            "why":             r.why,
        }
        for r in results
    ]

    if not dry_run:
        store_timelog_verification(db, target_date, None, results_dict)

    return len(results), "stored"


# =============================================================================
# MAIN
# =============================================================================

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backfill time log verification data from HCSS into the DB cache"
    )
    parser.add_argument(
        "--start",
        default="2026-01-01",
        help="Start date YYYY-MM-DD (default: 2026-01-01)",
    )
    parser.add_argument(
        "--end",
        default=date.today().isoformat(),
        help="End date YYYY-MM-DD (default: today)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would happen without writing anything to the DB",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-fetch and overwrite dates that already have cached data",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=1.0,
        help="Seconds to wait between dates (default: 1.0 — raise to 2-3 if you see 429 errors)",
    )
    args = parser.parse_args()

    start_date = date.fromisoformat(args.start)
    end_date   = date.fromisoformat(args.end)

    if end_date < start_date:
        print("ERROR: --end must be on or after --start")
        sys.exit(1)

    dates = build_date_list(start_date, end_date)
    est_minutes = len(dates) * (args.delay + 8) / 60  # ~8s per date for HCSS calls

    # ── Header ────────────────────────────────────────────────────────────────
    print()
    print("=" * 70)
    print("  TIME LOG VERIFICATION — BACKFILL")
    print("=" * 70)
    print(f"  Start date      : {args.start}")
    print(f"  End date        : {args.end}")
    print(f"  Total days      : {len(dates)}  (every calendar day, no exclusions)")
    print(f"  Force overwrite : {'YES — will re-fetch already-cached dates' if args.force else 'no'}")
    print(f"  Dry run         : {'YES — nothing will be written to DB' if args.dry_run else 'no'}")
    print(f"  Delay per date  : {args.delay}s")
    print(f"  Est. runtime    : ~{est_minutes:.0f} min  (varies with HCSS response time)")
    print("=" * 70)
    print()

    if not args.dry_run:
        confirm = input("  Proceed? [y/N] ").strip().lower()
        if confirm != "y":
            print("  Aborted.")
            sys.exit(0)
        print()

    # ── Setup ─────────────────────────────────────────────────────────────────
    create_tables()
    db = SessionLocal()

    stored        = 0
    empty         = 0
    skipped_cache = 0
    errors        = 0
    total_records = 0
    approved_total = 0
    flagged_total  = 0
    rejected_total = 0

    start_ts = time.perf_counter()

    # ── Process each date ─────────────────────────────────────────────────────
    try:
        for i, target_date in enumerate(dates, 1):
            print(f"  [{i:>3}/{len(dates)}] {target_date}", end=" ... ", flush=True)

            count, status = process_one_date(
                db,
                target_date,
                dry_run=args.dry_run,
                force=args.force,
            )

            if status == "stored":
                stored        += 1
                total_records += count
                if not args.dry_run:
                    entry = db.query(TimelogVerificationCache).filter(
                        and_(
                            TimelogVerificationCache.verification_date == target_date,
                            TimelogVerificationCache.business_unit_id == None,  # noqa: E711
                        )
                    ).first()
                    if entry:
                        approved_total += entry.approved_count
                        flagged_total  += entry.flagged_count
                        rejected_total += entry.rejected_count
                        print(
                            f"{count:>3} timecards  "
                            f"✓{entry.approved_count} approved  "
                            f"⚑{entry.flagged_count} flagged  "
                            f"✗{entry.rejected_count} rejected"
                        )
                    else:
                        print(f"{count:>3} timecards")
                else:
                    print(f"{count:>3} timecards  (dry run)")

            elif status == "skipped":
                skipped_cache += 1
                print("already cached — skipped  (use --force to overwrite)")

            elif status == "empty":
                empty += 1
                print("0 timecards  (no activity)")

            else:
                errors += 1
                print("ERROR — check logs above")

            if i < len(dates):
                time.sleep(args.delay)

    except KeyboardInterrupt:
        print()
        print()
        print("  Interrupted — partial results have been saved.")
        print("  Re-run without --force to continue from where it stopped.")
    finally:
        db.close()

    elapsed = time.perf_counter() - start_ts

    # ── Summary ───────────────────────────────────────────────────────────────
    print()
    print("=" * 70)
    print("  BACKFILL COMPLETE")
    print("=" * 70)
    print(f"  Elapsed           : {elapsed / 60:.1f} min  ({elapsed:.0f}s)")
    print(f"  Dates stored      : {stored}  ({total_records} total timecards)")
    if not args.dry_run and stored:
        print(f"    ✓ Approved      : {approved_total}")
        print(f"    ⚑ Flagged       : {flagged_total}")
        print(f"    ✗ Rejected      : {rejected_total}")
    print(f"  Dates skipped     : {skipped_cache}  (already in DB)")
    print(f"  Dates with no data: {empty}  (0 timecards returned from HCSS)")
    print(f"  Errors            : {errors}")
    if args.dry_run:
        print()
        print("  DRY RUN — nothing was written. Remove --dry-run to populate.")
    if errors:
        print()
        print("  Some dates errored. Re-run after fixing — already-stored dates")
        print("  will be skipped automatically.")
    print("=" * 70)
    print()


if __name__ == "__main__":
    main()
