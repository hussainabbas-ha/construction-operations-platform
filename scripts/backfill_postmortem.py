"""
scripts/backfill_postmortem.py
────────────────────────────────
Populate (or fully replace) the postmortem DB tables for all active HCSS jobs.

Strategy:
  1. Fetch all active jobs from HCSS (status == "active", isDeleted == false).
  2. Store / update each job's snapshot + cost-code rows.
  3. DELETE any snapshot rows whose job_id is NOT in the active set.
     (PostmortemCostCodeResult rows cascade-delete automatically.)

This ensures the DB always mirrors exactly the current set of active HCSS
jobs — no stale inactive/completed jobs ever remain.

Usage:
    python scripts/backfill_postmortem.py
    python scripts/backfill_postmortem.py --dry-run
    python scripts/backfill_postmortem.py --job-code 25006 25048
    python scripts/backfill_postmortem.py --debug
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from dotenv import load_dotenv
load_dotenv(_REPO_ROOT / ".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)-8s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Backfill postmortem DB tables from HCSS — active jobs only. "
            "Stale (inactive/completed/deleted) jobs are removed automatically."
        ),
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Fetch and compute data but do not write to or purge the database.",
    )
    parser.add_argument(
        "--job-code", nargs="+", metavar="CODE",
        help="Only process specific job codes. Stale purge is skipped when this flag is used.",
    )
    parser.add_argument(
        "--debug", action="store_true",
        help="Enable DEBUG-level logging.",
    )
    args = parser.parse_args()

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    print("\n" + "=" * 72)
    print("  Postmortem Backfill")
    print("=" * 72)

    from api.database import SessionLocal, create_tables
    from api.database import PostmortemJobSnapshot
    from api.db_services import store_postmortem_data
    from postmortem.data_fetcher import fetch_all_jobs_postmortem

    # Ensure tables exist
    create_tables()

    # ── Step 1: Fetch active jobs from HCSS ───────────────────────────────
    logger.info("Fetching active postmortem data from HCSS ...")
    job_results = fetch_all_jobs_postmortem()

    if not job_results:
        print("\n  No active jobs returned from HCSS. Nothing to store.")
        print("  Tip: check that HCSS credentials are correct and jobs have status='active'.")
        return

    # ── Step 2: Optional filter by job code ───────────────────────────────
    partial_run = False
    if args.job_code:
        partial_run = True
        requested = {str(c).strip() for c in args.job_code}
        job_results = [j for j in job_results if j["job_code"] in requested]
        if not job_results:
            print(f"\n  No matching active jobs found for: {', '.join(requested)}")
            return
        print(f"\n  Filtered to {len(job_results)} job(s): "
              f"{', '.join(j['job_code'] for j in job_results)}")

    print(f"\n  {len(job_results)} active job(s) fetched from HCSS\n")

    # ── Step 3: Dry-run output ─────────────────────────────────────────────
    if args.dry_run:
        print("  DRY RUN — no data will be written or deleted.\n")
        for job in job_results:
            t = job["totals"]
            print(
                f"  {job['job_code']:<12} "
                f"expected=${t['expected_all_cost']:>14,.2f}  "
                f"actual=${t['actual_cost']:>14,.2f}  "
                f"variance=${t['variance']:>14,.2f}  "
                f"cc={len(job['cost_codes'])}"
            )
        print(f"\n  Dry run complete — {len(job_results)} job(s) would be stored.")
        return

    # ── Step 4: Store each active job ─────────────────────────────────────
    db = SessionLocal()
    stored = 0
    failed = 0
    active_job_ids: list[str] = []

    try:
        for job in job_results:
            jid = job["job_id"]
            try:
                cc_count = store_postmortem_data(
                    db            = db,
                    job_id        = jid,
                    job_code      = job["job_code"],
                    job_name      = job["job_name"],
                    business_unit = job.get("business_unit"),
                    totals        = job["totals"],
                    cost_codes    = job["cost_codes"],
                )
                t = job["totals"]
                print(
                    f"  ✓ {job['job_code']:<12} "
                    f"expected=${t['expected_all_cost']:>14,.2f}  "
                    f"actual=${t['actual_cost']:>14,.2f}  "
                    f"variance=${t['variance']:>14,.2f}  "
                    f"cc={cc_count}"
                )
                active_job_ids.append(jid)
                stored += 1
            except Exception as exc:
                logger.error(
                    "Failed to store job %s (%s): %s",
                    job.get("job_code"), jid, exc,
                )
                print(f"  ✗ {job.get('job_code', '?'):<12} ERROR: {exc}")
                failed += 1

        # ── Step 5: Purge stale jobs ───────────────────────────────────────
        # Skip purge when processing a subset via --job-code, so we don't
        # accidentally wipe jobs we didn't intend to touch.
        purged = 0
        if not partial_run and active_job_ids:
            print("\n  Purging stale (inactive/completed/deleted) jobs from DB ...")
            stale_snapshots = (
                db.query(PostmortemJobSnapshot)
                .filter(PostmortemJobSnapshot.job_id.notin_(active_job_ids))
                .all()
            )
            if stale_snapshots:
                stale_codes = [s.job_code for s in stale_snapshots]
                logger.info(
                    "Purging %d stale snapshot(s): %s",
                    len(stale_snapshots),
                    ", ".join(stale_codes[:20]) + ("…" if len(stale_codes) > 20 else ""),
                )
                for snap in stale_snapshots:
                    db.delete(snap)  # cascades to postmortem_cost_code_results
                db.commit()
                purged = len(stale_snapshots)
                print(f"  ✓ Purged {purged} stale job snapshot(s)")
            else:
                print("  ✓ No stale jobs to purge")

    finally:
        db.close()

    # ── Summary ────────────────────────────────────────────────────────────
    print(f"\n{'=' * 72}")
    print(f"  Stored  : {stored}")
    print(f"  Failed  : {failed}")
    if not partial_run:
        print(f"  Purged  : {purged} stale job(s)")
    print(f"{'=' * 72}")
    print(f"\n  DB now contains exactly {stored} active job(s).\n")


if __name__ == "__main__":
    main()
