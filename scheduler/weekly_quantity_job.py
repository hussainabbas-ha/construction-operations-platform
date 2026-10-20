"""
Weekly quantity tracking alert job — runs every Friday at 8:00 AM US Mountain Time.

Checks all cost codes across all active jobs for the past 7 days.
If any item has reached 75%+ of its planned SOV/BI quantity, sends an
alert email to all NOTIFY_* recipients.
"""
from __future__ import annotations

import logging
import sys
from datetime import date, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from notifications.individual_email_sender import (
    get_all_notify_recipients,
    send_individual_email,
    send_no_data_alert,
    send_test_confirmation,
)
from notifications.quantity_email_template import build_quantity_alert_email
from quantity_tracking.tracker import track_quantities

_MT = ZoneInfo("America/Denver")
logger = logging.getLogger(__name__)

_JOB_NAME = "Weekly Quantity Alert"


def run_weekly_quantity_alerts() -> None:
    """
    1. Track quantities for the past 7 days.
    2. Filter items with alert=True.
    3. If no results at all, send no-data alert to test address.
    4. If no alerts, send only test confirmation.
    5. If alerts exist, send alert email to team and confirmation to test address.
    """
    today = date.today()
    start_date = (today - timedelta(days=7)).isoformat()
    end_date = today.isoformat()

    logger.info(
        "Weekly quantity alert job started: period=%s to %s",
        start_date,
        end_date,
    )

    try:
        results = track_quantities(start_date=start_date, end_date=end_date)
    except Exception as exc:
        reason = f"HCSS quantity tracking failed: {exc}"
        logger.error("Weekly quantity job aborted: %s", reason)
        send_no_data_alert(
            job_name=_JOB_NAME,
            report_date=f"{start_date} to {end_date}",
            reason=reason,
        )
        return

    alerts = [r for r in results if r.alert]

    logger.info(
        "Weekly quantity job: total_cost_codes=%d alerts=%d",
        len(results),
        len(alerts),
    )

    if not results:
        reason = (
            f"No quantity data returned from HCSS for period {start_date} to {end_date}. "
            "There may be no active jobs this week, or HCSS API may be unavailable."
        )
        logger.warning("Weekly quantity job: no results from HCSS — %s", reason)
        send_no_data_alert(
            job_name=_JOB_NAME,
            report_date=f"{start_date} to {end_date}",
            reason=reason,
        )
        return

    if not alerts:
        logger.info(
            "No quantity alerts this week — all %d cost codes under 75%%. "
            "No team email sent.",
            len(results),
        )
        send_test_confirmation(
            job_name=_JOB_NAME,
            report_date=f"{start_date} to {end_date}",
            recipients_count=0,
            extra_detail=(
                f"All {len(results)} cost code(s) are under 75% completion. "
                "No alert email was sent to the team."
            ),
        )
        return

    recipients = get_all_notify_recipients()
    if not recipients:
        logger.error(
            "No recipients configured — set NOTIFY_SUPERVISOR_EMAIL, "
            "NOTIFY_PM_EMAIL, or NOTIFY_ADMIN_EMAIL in .env"
        )
        send_no_data_alert(
            job_name=_JOB_NAME,
            report_date=f"{start_date} to {end_date}",
            reason=(
                f"{len(alerts)} alert(s) found but no recipients are configured. "
                "Set NOTIFY_* env variables."
            ),
        )
        return

    subject, html_body = build_quantity_alert_email(alerts, start_date, end_date)

    try:
        send_individual_email(
            subject=subject,
            html_body=html_body,
            recipients=recipients,
        )
        logger.info(
            "Weekly quantity alert sent to %d recipient(s): %s",
            len(recipients),
            ", ".join(recipients),
        )
    except Exception as exc:
        logger.exception("Failed to send weekly quantity alert: %s", exc)
        send_no_data_alert(
            job_name=_JOB_NAME,
            report_date=f"{start_date} to {end_date}",
            reason=f"Email send failed (SMTP error): {exc}",
        )
        return

    over_risk = sum(1 for r in alerts if r.status == "OVER_RISK")
    near_completion = sum(1 for r in alerts if r.status == "NEAR_COMPLETION")

    send_test_confirmation(
        job_name=_JOB_NAME,
        report_date=f"{start_date} to {end_date}",
        recipients_count=len(recipients),
        extra_detail=(
            f"Alerts — Over Risk: {over_risk} | Near Completion: {near_completion} "
            f"| Total Alerted: {len(alerts)} of {len(results)} cost codes"
        ),
    )


def create_quantity_scheduler() -> BlockingScheduler:
    scheduler = BlockingScheduler(timezone=_MT)
    scheduler.add_job(
        run_weekly_quantity_alerts,
        CronTrigger(
            day_of_week="fri",
            hour=8,
            minute=0,
            second=0,
            timezone=_MT,
        ),
        id="weekly_quantity_alerts",
        replace_existing=True,
    )
    logger.info("Standalone scheduler created — weekly job fires every Friday at 08:00 MT")
    return scheduler


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    logger.info("Starting Gould APM Weekly Quantity Scheduler (standalone)")
    create_quantity_scheduler().start()


if __name__ == "__main__":
    main()
