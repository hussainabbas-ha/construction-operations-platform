"""
Manual test script — Daily Time Log Report Email

Run this to immediately send the daily time log report email
without waiting for the 1 PM scheduler to fire.

Usage:
    python test_daily_email.py              # uses yesterday's date
    python test_daily_email.py 2026-05-17   # uses a specific date

Recipients are read from .env (NOTIFY_SUPERVISOR_EMAIL, NOTIFY_PM_EMAIL, NOTIFY_ADMIN_EMAIL).
A test confirmation is also sent to NOTIFY_TEST_EMAIL.
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

# Make sure repo root is on the path
_REPO_ROOT = Path(__file__).resolve().parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from dotenv import load_dotenv
load_dotenv(_REPO_ROOT / ".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)-8s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("test_daily_email")


def main() -> None:
    # Allow optional date argument
    if len(sys.argv) > 1:
        target_date = sys.argv[1]
        # Validate format
        from datetime import date
        try:
            date.fromisoformat(target_date)
        except ValueError:
            print(f"ERROR: Invalid date '{target_date}'. Use YYYY-MM-DD format.")
            sys.exit(1)
    else:
        from datetime import datetime, timedelta
        from zoneinfo import ZoneInfo
        target_date = (datetime.now(ZoneInfo("America/Denver")).date() - timedelta(days=1)).isoformat()

    print("=" * 60)
    print("  Gould APM — Daily Time Log Email Test")
    print("=" * 60)
    print(f"  Target date : {target_date}")

    # Show recipients from env
    from notifications.individual_email_sender import (
        get_all_notify_recipients,
        get_test_email,
    )
    recipients = get_all_notify_recipients()
    test_email = get_test_email()

    print(f"  Team recipients ({len(recipients)}):")
    for r in recipients:
        print(f"    - {r}")
    print(f"  Test/monitor  : {test_email or '(not set — NOTIFY_TEST_EMAIL is empty)'}")
    print("=" * 60)

    if not recipients:
        print("\nERROR: No recipients configured.")
        print("Set NOTIFY_SUPERVISOR_EMAIL / NOTIFY_PM_EMAIL / NOTIFY_ADMIN_EMAIL in .env")
        sys.exit(1)

    # Load results from DB cache
    print(f"\nStep 1 — Loading results from DB cache for {target_date}...")
    from api.cache_service import get_cached_verification
    from api.database import SessionLocal

    db = SessionLocal()
    try:
        cached = get_cached_verification(db, target_date, business_unit_id=None)
    finally:
        db.close()

    if cached is None:
        print(f"\nWARNING: No data in DB cache for {target_date}.")
        print("The unified sync job (runs every 8h) may not have fetched this date yet.")
        print("Options:")
        print("  1. Wait for the sync job to run (check logs).")
        print("  2. Try a date that already has data, e.g. python test_daily_email.py 2026-05-16")
        print("\nA no-data alert email has been sent to your test address (if configured).")

        from notifications.individual_email_sender import send_no_data_alert
        send_no_data_alert(
            job_name="Daily Time Log Report [MANUAL TEST]",
            report_date=target_date,
            reason=(
                f"Manual test script ran but found no DB cache for {target_date}. "
                "Unified sync job may not have run yet for this date."
            ),
        )
        sys.exit(1)

    _, results = cached
    print(f"  Found {len(results)} timecard(s) in DB cache.")

    if len(results) == 0:
        print(f"\nINFO: DB cache exists for {target_date} but has 0 timecards.")
        print("This is normal for weekends/holidays. No report email will be sent.")

        from notifications.individual_email_sender import send_no_data_alert
        send_no_data_alert(
            job_name="Daily Time Log Report [MANUAL TEST]",
            report_date=target_date,
            reason=f"0 timecards in DB for {target_date} (weekend or holiday).",
        )
        sys.exit(0)

    rejected = sum(1 for r in results if r.status == "REJECTED")
    flagged  = sum(1 for r in results if r.status == "FLAGGED")
    approved = sum(1 for r in results if r.status == "APPROVED")
    print(f"  Rejected: {rejected}  |  Flagged: {flagged}  |  Approved: {approved}")

    # Build email
    print("\nStep 2 — Building email...")
    from notifications.email_template import build_email_body
    subject, html_body = build_email_body(results, target_date)
    print(f"  Subject: {subject}")

    # Send to team
    print(f"\nStep 3 — Sending to {len(recipients)} recipient(s)...")
    from notifications.individual_email_sender import send_individual_email
    try:
        send_individual_email(
            subject=subject,
            html_body=html_body,
            recipients=recipients,
        )
        print("  ✓ Team email sent successfully.")
    except Exception as exc:
        print(f"  ✗ SMTP error: {exc}")
        sys.exit(1)

    # Send test confirmation
    if test_email:
        print(f"\nStep 4 — Sending confirmation to test address ({test_email})...")
        from notifications.individual_email_sender import send_test_confirmation
        send_test_confirmation(
            job_name="Daily Time Log Report [MANUAL TEST]",
            report_date=target_date,
            recipients_count=len(recipients),
            extra_detail=(
                f"Summary — Rejected: {rejected} | Flagged: {flagged} | "
                f"Approved: {approved} | Total: {len(results)}"
            ),
        )
        print("  ✓ Test confirmation sent.")
    else:
        print("\nStep 4 — Skipped (NOTIFY_TEST_EMAIL not set).")

    print("\n" + "=" * 60)
    print("  ALL DONE — daily email test completed successfully.")
    print("=" * 60)


if __name__ == "__main__":
    main()