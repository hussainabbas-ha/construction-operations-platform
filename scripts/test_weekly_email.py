"""
Manual test script — Weekly Quantity Alert Email

Run this to immediately send the weekly quantity alert email
without waiting for the Friday 8 AM scheduler to fire.

Usage:
    python test_weekly_email.py                          # past 7 days
    python test_weekly_email.py 2026-05-11 2026-05-17   # custom date range

Recipients are read from .env (NOTIFY_SUPERVISOR_EMAIL, NOTIFY_PM_EMAIL, NOTIFY_ADMIN_EMAIL).
A test confirmation is also sent to NOTIFY_TEST_EMAIL.

Note: This script fetches quantity data directly from HCSS (same as the
real weekly job). It does NOT use the DB cache.
"""
from __future__ import annotations

import logging
import sys
from datetime import date, timedelta
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
logger = logging.getLogger("test_weekly_email")


def _validate_date(d: str, label: str) -> None:
    try:
        date.fromisoformat(d)
    except ValueError:
        print(f"ERROR: Invalid {label} '{d}'. Use YYYY-MM-DD format.")
        sys.exit(1)


def main() -> None:
    # Resolve date range
    if len(sys.argv) == 3:
        start_date = sys.argv[1]
        end_date   = sys.argv[2]
        _validate_date(start_date, "start_date")
        _validate_date(end_date, "end_date")
        if end_date < start_date:
            print("ERROR: end_date must be on or after start_date.")
            sys.exit(1)
    elif len(sys.argv) == 1:
        today      = date.today()
        start_date = (today - timedelta(days=7)).isoformat()
        end_date   = today.isoformat()
    else:
        print("Usage:")
        print("  python test_weekly_email.py                          # past 7 days")
        print("  python test_weekly_email.py 2026-05-11 2026-05-17   # custom range")
        sys.exit(1)

    print("=" * 60)
    print("  Gould APM — Weekly Quantity Alert Email Test")
    print("=" * 60)
    print(f"  Period      : {start_date} to {end_date}")

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

    # Fetch quantity data from HCSS
    print(f"\nStep 1 — Fetching quantity data from HCSS for {start_date} to {end_date}...")
    print("  (This may take 30-60 seconds depending on active jobs)")

    from quantity_tracking.tracker import track_quantities
    try:
        results = track_quantities(start_date=start_date, end_date=end_date)
    except Exception as exc:
        print(f"\n  ✗ HCSS fetch failed: {exc}")
        from notifications.individual_email_sender import send_no_data_alert
        send_no_data_alert(
            job_name="Weekly Quantity Alert [MANUAL TEST]",
            report_date=f"{start_date} to {end_date}",
            reason=f"HCSS fetch failed: {exc}",
        )
        sys.exit(1)

    print(f"  Found {len(results)} cost code(s) total.")

    if not results:
        print("\nINFO: No quantity data returned from HCSS for this period.")
        print("There may be no active jobs, or HCSS may be unavailable.")

        from notifications.individual_email_sender import send_no_data_alert
        send_no_data_alert(
            job_name="Weekly Quantity Alert [MANUAL TEST]",
            report_date=f"{start_date} to {end_date}",
            reason="No quantity data returned from HCSS for this period.",
        )
        sys.exit(0)

    # Filter alerts
    alerts = [r for r in results if r.alert]
    on_track    = sum(1 for r in results if r.status == "ON_TRACK")
    near        = sum(1 for r in results if r.status == "NEAR_COMPLETION")
    over        = sum(1 for r in results if r.status == "OVER_RISK")

    print(f"  On Track: {on_track}  |  Near Completion: {near}  |  Over Risk: {over}")
    print(f"  Alerts (>=75%): {len(alerts)}")

    if not alerts:
        print("\nINFO: No cost codes at or above 75%. No alert email will be sent to the team.")
        print("A confirmation email will still be sent to your test address.")

        from notifications.individual_email_sender import send_test_confirmation
        send_test_confirmation(
            job_name="Weekly Quantity Alert [MANUAL TEST]",
            report_date=f"{start_date} to {end_date}",
            recipients_count=0,
            extra_detail=(
                f"All {len(results)} cost code(s) are under 75% completion. "
                "No alert email was sent to the team."
            ),
        )
        print("\n" + "=" * 60)
        print("  DONE — no alerts to send.")
        print("=" * 60)
        sys.exit(0)

    # Build email
    print("\nStep 2 — Building alert email...")
    from scheduler.weekly_quantity_job import _build_quantity_alert_email
    subject, html_body = _build_quantity_alert_email(alerts, start_date, end_date)
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
        print("  ✓ Alert email sent successfully.")
    except Exception as exc:
        print(f"  ✗ SMTP error: {exc}")
        from notifications.individual_email_sender import send_no_data_alert
        send_no_data_alert(
            job_name="Weekly Quantity Alert [MANUAL TEST]",
            report_date=f"{start_date} to {end_date}",
            reason=f"Email send failed (SMTP error): {exc}",
        )
        sys.exit(1)

    # Send test confirmation
    if test_email:
        print(f"\nStep 4 — Sending confirmation to test address ({test_email})...")
        from notifications.individual_email_sender import send_test_confirmation
        send_test_confirmation(
            job_name="Weekly Quantity Alert [MANUAL TEST]",
            report_date=f"{start_date} to {end_date}",
            recipients_count=len(recipients),
            extra_detail=(
                f"Alerts — Over Risk: {over} | Near Completion: {near} "
                f"| Total Alerted: {len(alerts)} of {len(results)} cost codes"
            ),
        )
        print("  ✓ Test confirmation sent.")
    else:
        print("\nStep 4 — Skipped (NOTIFY_TEST_EMAIL not set).")

    print("\n" + "=" * 60)
    print("  ALL DONE — weekly email test completed successfully.")
    print("=" * 60)


if __name__ == "__main__":
    main()