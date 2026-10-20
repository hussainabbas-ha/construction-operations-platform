"""
Backfill production quantity tracking with the daily active-jobs model.

For each calendar day:
  1. Use one fixed list of active, non-deleted HCSS jobs.
  2. Reuse cost-code metadata fetched once for the run.
  3. Sum progress["quantity"] for that day.
  4. Store all cost codes (including planned <= 0); display filter is API-only.
  5. Overwrite that day's quantity_tracking_cache/results rows.

By default the end date is yesterday so an incomplete current day is never
written into production.

Usage:
  python scripts/backfill_quantity_production.py --start 2026-04-01
  python scripts/backfill_quantity_production.py --start 2026-04-01 --end 2026-07-21
  python scripts/backfill_quantity_production.py --start 2026-04-01 --resume
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, timedelta
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from api.database import SessionLocal, create_tables  # noqa: E402
from api.db_services import (  # noqa: E402
    quantity_day_present,
    store_quantity_tracking,
)
from payroll_verification.hcss_client import HCSSClient  # noqa: E402
from scripts.populate_quantity_test import (  # noqa: E402
    _date_list,
    build_day_rows,
    fetch_active_jobs,
    fetch_cost_code_meta,
    progress_for_day,
    resolve_job_codes,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)-8s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("backfill_quantity_production")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backfill production quantity tracking with daily HCSS progress"
    )
    parser.add_argument(
        "--start",
        default="2026-04-01",
        help="First day YYYY-MM-DD (default: 2026-04-01)",
    )
    parser.add_argument(
        "--end",
        default=(date.today() - timedelta(days=1)).isoformat(),
        help="Last day YYYY-MM-DD (default: yesterday)",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Skip days that already have a production cache header",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=0.35,
        help="Seconds between HCSS calls (default: 0.35)",
    )
    args = parser.parse_args()

    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)
    yesterday = date.today() - timedelta(days=1)
    if start > end:
        raise SystemExit("--start must be <= --end")
    if end > yesterday:
        raise SystemExit(
            f"--end cannot exceed yesterday ({yesterday.isoformat()}); "
            "today's quantity is incomplete"
        )

    days = _date_list(start.isoformat(), end.isoformat())
    create_tables()
    client = HCSSClient()

    print("=" * 78, flush=True)
    print("PRODUCTION QUANTITY DAILY BACKFILL", flush=True)
    print("=" * 78, flush=True)
    print(f"Range       : {start} -> {end} ({len(days)} days)", flush=True)
    print(f"Resume      : {args.resume}", flush=True)
    print(f"Call delay  : {args.delay}s", flush=True)

    print("\n[setup] Fetching active HCSS jobs...", flush=True)
    active_jobs = fetch_active_jobs(client)
    jobs = [j for j in active_jobs if j.get("id")]
    job_ids = [str(j["id"]) for j in jobs]
    if not job_ids:
        raise SystemExit("No active HCSS jobs found; production was not modified.")

    job_code_map = {
        str(j["id"]): str(j.get("code") or j.get("jobCode") or "")
        for j in jobs
    }
    missing_codes = [jid for jid in job_ids if not job_code_map.get(jid)]
    if missing_codes:
        job_code_map.update(resolve_job_codes(client, missing_codes))

    print(f"[setup] Active jobs: {len(job_ids)}", flush=True)
    print("[setup] Fetching cost-code metadata once...", flush=True)
    cc_meta = fetch_cost_code_meta(client, job_ids, args.delay)
    planned_count = sum(1 for item in cc_meta.values() if item["planned"] > 0)
    print(
        f"[setup] Cost codes: {len(cc_meta)} "
        f"({planned_count} with planned > 0)",
        flush=True,
    )
    if not cc_meta:
        raise SystemExit("No cost-code metadata returned; production was not modified.")

    db = SessionLocal()
    completed = skipped = failed = 0
    try:
        for index, day in enumerate(days, 1):
            if args.resume and quantity_day_present(db, day):
                skipped += 1
                print(
                    f"[{index}/{len(days)}] {day}: SKIP (already present)",
                    flush=True,
                )
                continue

            print(f"[{index}/{len(days)}] {day}: fetching...", flush=True)
            try:
                installed = progress_for_day(client, job_ids, day, args.delay)
                rows = build_day_rows(installed, cc_meta, job_code_map)
                nonzero = sum(
                    1 for row in rows if row["installed_quantity"] != 0
                )
                _, stored = store_quantity_tracking(db, day, rows)
                completed += 1
                print(
                    f"[{index}/{len(days)}] {day}: STORED {stored} "
                    f"({nonzero} nonzero)",
                    flush=True,
                )
            except Exception as exc:
                failed += 1
                db.rollback()
                logger.exception("Backfill failed for %s: %s", day, exc)
                print(
                    f"[{index}/{len(days)}] {day}: FAILED ({exc})",
                    flush=True,
                )
    finally:
        db.close()

    print("=" * 78, flush=True)
    print(
        f"BACKFILL COMPLETE: stored={completed} skipped={skipped} failed={failed}",
        flush=True,
    )
    print("=" * 78, flush=True)

    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
