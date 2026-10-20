"""
test_all_emails.py

Runs production-style scheduler emails manually.

Supported filters:
  python test_all_emails.py --date 2026-06-02
  python test_all_emails.py --start-date 2026-06-01 --end-date 2026-06-18
  python test_all_emails.py --job timelog
  python test_all_emails.py --job budget_alert
  python test_all_emails.py --job budget_summary
  python test_all_emails.py --job quantity
  python test_all_emails.py --job projection
  python test_all_emails.py --job projection --date 2026-06-18
  python test_all_emails.py --job projection --start-date 2026-06-01 --end-date 2026-06-18
  python test_all_emails.py --recipients you@email.com,other@email.com
  python test_all_emails.py --use-real-recipients
  python test_all_emails.py --force-hcss
  python test_all_emails.py --dry-run

Important:
  - No [TEST] prefix is added.
  - No [] prefix is added.
  - No "Manual Test" wording is added.
  - Budget and projection use the same route helper logic as the module/dashboard.
  - Budget/projection route helpers already read from DB first and fetch from HCSS on cache miss.
"""

from __future__ import annotations

import argparse
import os
import sys
import traceback
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

_REPO_ROOT = Path(__file__).resolve().parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

try:
    from dotenv import load_dotenv

    load_dotenv(_REPO_ROOT / ".env")
except Exception:
    pass

_MT = ZoneInfo("America/Denver")


def _yesterday() -> str:
    return (datetime.now(_MT).date() - timedelta(days=1)).isoformat()


def _validate_date(date_str: str, label: str) -> str:
    try:
        datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError as exc:
        raise SystemExit(f"Invalid {label}: {date_str!r}. Expected YYYY-MM-DD.") from exc
    return date_str


def _resolve_recipients(args: argparse.Namespace) -> list[str]:
    """
    Recipient priority:
      1. --recipients email1,email2
      2. --use-real-recipients -> NOTIFY_* roles + NOTIFY_EXTRA_EMAILS
      3. default -> NOTIFY_TEST_EMAIL
      4. fallback -> real NOTIFY_* recipients
    """
    if args.recipients:
        recipients = [r.strip() for r in args.recipients.split(",") if r.strip()]
        if not recipients:
            raise SystemExit("--recipients was provided, but no valid emails were found.")
        return recipients

    from notifications.individual_email_sender import get_all_notify_recipients

    if args.use_real_recipients:
        recipients = get_all_notify_recipients()
        if not recipients:
            raise SystemExit(
                "--use-real-recipients was used, but no real NOTIFY_* recipients are configured."
            )
        return recipients

    test_addr = os.environ.get("NOTIFY_TEST_EMAIL", "").strip()
    if test_addr:
        return [test_addr]

    recipients = get_all_notify_recipients()
    if recipients:
        print("NOTIFY_TEST_EMAIL is not set. Falling back to real NOTIFY_* recipients.")
        return recipients

    raise SystemExit(
        "No recipients found. Use --recipients or configure NOTIFY_TEST_EMAIL / NOTIFY_* emails."
    )


def _send_budget(subject: str, html_body: str, recipients: list[str], dry_run: bool) -> None:
    if dry_run:
        print("DRY RUN — budget sender would send:")
        print(f"  Subject: {subject}")
        print(f"  To: {', '.join(recipients)}")
        return

    from notifications.budget_email_sender import send_budget_email

    send_budget_email(
        subject=subject,
        html_body=html_body,
        recipients=recipients,
    )


def _send_individual(subject: str, html_body: str, recipients: list[str], dry_run: bool) -> None:
    if dry_run:
        print("DRY RUN — individual sender would send:")
        print(f"  Subject: {subject}")
        print(f"  To: {', '.join(recipients)}")
        return

    from notifications.individual_email_sender import send_individual_email

    send_individual_email(
        subject=subject,
        html_body=html_body,
        recipients=recipients,
    )


def run_timelog(
    target_date: str,
    recipients: list[str],
    dry_run: bool,
    force_hcss: bool,
) -> bool:
    try:
        results = None

        if not force_hcss:
            from api.cache_service import get_cached_verification
            from api.database import SessionLocal

            db = SessionLocal()
            try:
                cached = get_cached_verification(db, target_date, business_unit_id=None)
            finally:
                db.close()

            if cached:
                _, results = cached
                print(f"Time log DB cache hit: {len(results)} row(s)")
            else:
                print("Time log DB cache miss.")

        if results is None:
            print("Fetching time log data from HCSS...")
            from payroll_verification.verifier import verify_payroll_date

            results = verify_payroll_date(
                target_date=target_date,
                business_unit_id=None,
            )

        if not results:
            print(f"No time log data found for {target_date}. No email sent.")
            return True

        from notifications.email_template import build_email_body

        subject, html_body = build_email_body(results, target_date)
        _send_individual(subject, html_body, recipients, dry_run)
        print("Time log email completed.")
        return True

    except Exception:
        traceback.print_exc()
        return False


def run_budget_alert(
    target_date: str,
    recipients: list[str],
    dry_run: bool,
) -> bool:
    try:
        from api.routes.budget import _fetch_for_range, _to_budget_item_row
        from notifications.budget_email_template import build_budget_email_body

        items, qty_lookup = _fetch_for_range(
            target_date,
            target_date,
            request_id="email-runner",
        )

        over_risk_items = [item for item in items if item.get("status") == "OVER_RISK"]

        if not over_risk_items:
            print(f"No OVER_RISK budget rows found for {target_date}. No email sent.")
            return True

        rows = [_to_budget_item_row(item, qty_lookup) for item in over_risk_items]

        subject, html_body = build_budget_email_body(
            period_start=target_date,
            period_end=target_date,
            items=rows,
            comments=(
                f"DAILY ALERT — {len(over_risk_items)} cost code(s) have reached "
                "budget risk level. Immediate review required."
            ),
        )

        subject = (
            f"Budget Alert — {len(over_risk_items)} OVER RISK item(s) require review "
            f"| {target_date}"
        )

        _send_budget(subject, html_body, recipients, dry_run)
        print("Budget alert email completed.")
        return True

    except Exception:
        traceback.print_exc()
        return False


def run_budget_summary(
    target_date: str,
    recipients: list[str],
    dry_run: bool,
) -> bool:
    try:
        from api.routes.budget import _fetch_for_range, _to_budget_item_row
        from notifications.budget_email_template import build_budget_email_body

        end_date = target_date
        start_date = (datetime.fromisoformat(target_date) - timedelta(days=7)).strftime(
            "%Y-%m-%d"
        )

        items, qty_lookup = _fetch_for_range(
            start_date,
            end_date,
            request_id="email-runner",
        )

        if not items:
            print(f"No budget summary data found for {start_date} to {end_date}. No email sent.")
            return True

        rows = [_to_budget_item_row(item, qty_lookup) for item in items]

        over_risk = sum(1 for item in items if item.get("status") == "OVER_RISK")
        on_track = sum(1 for item in items if item.get("status") == "ON_TRACK")

        subject, html_body = build_budget_email_body(
            period_start=start_date,
            period_end=end_date,
            items=rows,
            comments="",
        )

        subject = (
            f"Weekly Budget Summary — {over_risk} Over Risk, {on_track} On Track "
            f"| {start_date} to {end_date}"
        )

        _send_budget(subject, html_body, recipients, dry_run)
        print("Budget summary email completed.")
        return True

    except Exception:
        traceback.print_exc()
        return False


def run_quantity(
    target_date: str,
    recipients: list[str],
    dry_run: bool,
) -> bool:
    try:
        from notifications.quantity_email_template import build_quantity_alert_email
        from quantity_tracking.tracker import track_quantities

        end_date = target_date
        start_date = (datetime.fromisoformat(target_date) - timedelta(days=7)).strftime(
            "%Y-%m-%d"
        )

        results = track_quantities(
            start_date=start_date,
            end_date=end_date,
        )

        if not results:
            print(f"No quantity data found for {start_date} to {end_date}. No email sent.")
            return True

        alerts = [row for row in results if row.alert]

        if not alerts:
            print(f"No quantity alerts found for {start_date} to {end_date}. No email sent.")
            return True

        subject, html_body = build_quantity_alert_email(alerts, start_date, end_date)

        _send_individual(subject, html_body, recipients, dry_run)
        print("Quantity alert email completed.")
        return True

    except Exception:
        traceback.print_exc()
        return False


def run_projection_range(
    start_date: str,
    end_date: str,
    recipients: list[str],
    dry_run: bool,
) -> bool:
    try:
        from api.routes.projection_routes import ProjectionItemRow, _fetch_for_range
        from notifications.projection_email_template import build_projection_email_body

        period_label = start_date if start_date == end_date else f"{start_date} to {end_date}"

        items = _fetch_for_range(
            "period",
            start_date,
            start_date,
            end_date,
            request_id="email-runner",
        )

        if not items:
            print(f"No projection data found for {period_label}. No email sent.")
            return True

        rows = [ProjectionItemRow(**item) for item in items]

        subject, html_body = build_projection_email_body(
            tracking_month=period_label,
            items=rows,
            comments="",
            triggered_by="Scheduled",
        )

        _send_budget(subject, html_body, recipients, dry_run)
        print("Projection email completed.")
        return True

    except Exception:
        traceback.print_exc()
        return False


def run_projection(
    target_date: str,
    recipients: list[str],
    dry_run: bool,
) -> bool:
    return run_projection_range(
        start_date=target_date,
        end_date=target_date,
        recipients=recipients,
        dry_run=dry_run,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run production-style scheduler emails manually.",
    )

    parser.add_argument(
        "--date",
        default=None,
        help="Single target date YYYY-MM-DD. Default: yesterday.",
    )
    parser.add_argument(
        "--start-date",
        default=None,
        help="Start date YYYY-MM-DD for projection range mode.",
    )
    parser.add_argument(
        "--end-date",
        default=None,
        help="End date YYYY-MM-DD for projection range mode.",
    )
    parser.add_argument(
        "--job",
        default="all",
        choices=[
            "all",
            "timelog",
            "budget_alert",
            "budget_summary",
            "quantity",
            "projection",
        ],
        help="Which email flow to run.",
    )
    parser.add_argument(
        "--recipients",
        default=None,
        help="Comma-separated recipient emails.",
    )
    parser.add_argument(
        "--use-real-recipients",
        action="store_true",
        help="Send to NOTIFY_SUPERVISOR_EMAIL / NOTIFY_PM_EMAIL / NOTIFY_ADMIN_EMAIL.",
    )
    parser.add_argument(
        "--force-hcss",
        action="store_true",
        help="Force HCSS fetch where supported. Budget/projection route helpers already fetch HCSS on cache miss.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Build/run flow and print subject/recipients without sending.",
    )

    return parser.parse_args()


def main() -> None:
    args = parse_args()

    if args.start_date or args.end_date:
        if not args.start_date or not args.end_date:
            print("Error: both --start-date and --end-date are required for range mode.")
            sys.exit(1)

        start_date = _validate_date(args.start_date, "--start-date")
        end_date = _validate_date(args.end_date, "--end-date")

        if end_date < start_date:
            print("Error: --end-date must be after or equal to --start-date.")
            sys.exit(1)

        target_date = end_date

    else:
        target_date = _validate_date(args.date, "--date") if args.date else _yesterday()
        start_date = target_date
        end_date = target_date

    recipients = _resolve_recipients(args)

    print(f"Date: {target_date}")
    print(f"Start Date: {start_date}")
    print(f"End Date: {end_date}")
    print(f"Job: {args.job}")
    print(f"Recipients: {recipients}")
    print(f"Force HCSS: {args.force_hcss}")
    print(f"Dry run: {args.dry_run}")

    if args.force_hcss:
        print(
            "Note: --force-hcss is used for time log. "
            "Budget and projection already fetch from HCSS on DB cache miss through route helpers."
        )

    results: list[bool] = []

    if args.job in ("all", "timelog"):
        results.append(
            run_timelog(
                target_date=target_date,
                recipients=recipients,
                dry_run=args.dry_run,
                force_hcss=args.force_hcss,
            )
        )

    if args.job in ("all", "budget_alert"):
        results.append(
            run_budget_alert(
                target_date=target_date,
                recipients=recipients,
                dry_run=args.dry_run,
            )
        )

    if args.job in ("all", "budget_summary"):
        results.append(
            run_budget_summary(
                target_date=target_date,
                recipients=recipients,
                dry_run=args.dry_run,
            )
        )

    if args.job in ("all", "quantity"):
        results.append(
            run_quantity(
                target_date=target_date,
                recipients=recipients,
                dry_run=args.dry_run,
            )
        )

    if args.job in ("all", "projection"):
        results.append(
            run_projection_range(
                start_date=start_date,
                end_date=end_date,
                recipients=recipients,
                dry_run=args.dry_run,
            )
        )

    if all(results):
        print("Completed successfully.")
        sys.exit(0)

    print("One or more jobs failed.")
    sys.exit(1)


if __name__ == "__main__":
    main()
