"""
scheduler/monthly_projection_job.py
─────────────────────────────────────
Monthly projection calculation and auto-alert job.

Runs on the 23rd of every month at 08:00 Mountain Time.
This date aligns with the client's billing cycle:
  - 23rd → projection calculated + billing draft prepared
  - 25th → final billing submitted

What this job does:
  1. Fetch projection data for the current month from HCSS
  2. Calculate EAC, cost variance, status per job
  3. Store results in the database
  4. If any jobs are OVER_BUDGET (15%+ overrun) → auto-send alert email to Accounting
  5. If any jobs have discrepancy flags → include in Accounting alert

This mirrors scheduler/weekly_quantity_job.py in structure.

NEW FILE — register in scheduler/background_scheduler.py (see bottom of file).
"""
from __future__ import annotations

import logging
import os
from datetime import datetime
from zoneinfo import ZoneInfo

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from notifications.projection_email_template import build_projection_email_body
from notifications.budget_email_sender import send_budget_email   # reuse SMTP sender
from notifications.individual_email_sender import get_recipient_directory

logger = logging.getLogger(__name__)

_MT = ZoneInfo("America/Denver")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _current_month() -> str:
    return datetime.now(_MT).strftime("%Y-%m")


def _get_accounting_recipients() -> list[str]:
    """
    Get accounting recipient emails from .env.

    Uses existing env vars + dedicated accounting var:
      NOTIFY_ACCOUNTING_EMAIL  — dedicated accounting recipient
      NOTIFY_ADMIN_EMAIL       — fallback (admin = accounting in small firms)
      NOTIFY_PM_EMAIL          — project managers should see over-budget alerts

    Add NOTIFY_ACCOUNTING_EMAIL to your .env file.
    """
    recipients = []

    accounting = os.environ.get("NOTIFY_ACCOUNTING_EMAIL", "").strip()
    if accounting:
        recipients.append(accounting)

    # Fallback: use admin email if no dedicated accounting email
    if not recipients:
        admin = os.environ.get("NOTIFY_ADMIN_EMAIL", "").strip()
        if admin:
            recipients.append(admin)

    # Always include PM for over-budget alerts (client doc requirement)
    pm = os.environ.get("NOTIFY_PM_EMAIL", "").strip()
    if pm and pm not in recipients:
        recipients.append(pm)

    return recipients


# ── Main job function ─────────────────────────────────────────────────────────

def run_monthly_projection_job() -> None:
    """
    Main monthly projection job.

    Called by APScheduler on the 23rd of every month.
    Also callable directly for testing.

    Steps:
        1. Calculate projections for current month
        2. Store in database
        3. Auto-send alert email if OVER_BUDGET or discrepancy jobs found
    """
    target_month = _current_month()
    logger.info(
        "Monthly projection job started: month=%s", target_month
    )

    # ── Step 1 & 2: Fetch + calculate + store ─────────────────────────────
    from api.database import SessionLocal, create_tables
    from api.db_services import store_projection_tracking
    from projection_tracking.hcss_client import ProjectionHCSSClient
    from projection_tracking.analyzer import calculate_projections

    create_tables()
    db = SessionLocal()

    try:
        raw_data = ProjectionHCSSClient().fetch_projection_data(month=target_month)
    except Exception as exc:
        logger.error(
            "Monthly projection job: failed to fetch HCSS data month=%s: %s",
            target_month, exc,
        )
        db.close()
        return

    if not raw_data:
        logger.warning(
            "Monthly projection job: no data returned from HCSS for month=%s",
            target_month,
        )
        db.close()
        return

    results = calculate_projections(raw_data)

    # Convert to dicts for storage
    results_dicts = [
        {
            "job_id": r.job_id,
            "job_code": r.job_code,
            "job_name": r.job_name,
            "business_unit": r.business_unit,
            "original_contract_value": r.original_contract_value,
            "approved_change_orders": r.approved_change_orders,
            "revised_contract_value": r.revised_contract_value,
            "original_budget": r.original_budget,
            "actual_cost_to_date": r.actual_cost_to_date,
            "projected_final_cost": r.projected_final_cost,
            "cost_variance": r.cost_variance,
            "percent_complete": r.percent_complete,
            "estimated_completion_month": r.estimated_completion_month,
            "billed_to_date": r.billed_to_date,
            "projected_final_billing": r.projected_final_billing,
            "billing_variance": r.billing_variance,
            "status": r.status,
            "alert": r.alert,
            "discrepancy_flag": r.discrepancy_flag,
        }
        for r in results
    ]

    try:
        store_projection_tracking(db, target_month, results_dicts)
        logger.info(
            "Monthly projection job: stored %d records for month=%s",
            len(results_dicts), target_month,
        )
    except Exception as exc:
        logger.error(
            "Monthly projection job: failed to store data month=%s: %s",
            target_month, exc,
        )
        db.close()
        return
    finally:
        db.close()

    # ── Step 3: Auto-send alert email if needed ────────────────────────────
    over_budget = [r for r in results if r.status == "OVER_BUDGET"]
    discrepancy = [r for r in results if r.discrepancy_flag]

    # Always send the monthly projection report to Accounting on the 23rd
    # (client doc: "Calculate monthly projections... prompt Accounting")
    recipients = _get_accounting_recipients()

    if not recipients:
        logger.warning(
            "Monthly projection job: no accounting recipients configured. "
            "Set NOTIFY_ACCOUNTING_EMAIL in .env. "
            "Skipping email for month=%s",
            target_month,
        )
        return

    # Import the Pydantic row model for the email builder
    from api.routes.projection_routes import ProjectionItemRow

    item_rows = [ProjectionItemRow(**d) for d in results_dicts]

    if over_budget or discrepancy:
        triggered_by = "Auto-Alert"
        logger.warning(
            "Monthly projection job: %d OVER_BUDGET and %d discrepancy jobs "
            "found for month=%s — sending alert to Accounting",
            len(over_budget), len(discrepancy), target_month,
        )
    else:
        triggered_by = "Scheduled"
        logger.info(
            "Monthly projection job: all jobs on track for month=%s "
            "— sending standard monthly report",
            target_month,
        )

    subject, html_body = build_projection_email_body(
        tracking_month=target_month,
        items=item_rows,
        comments="",
        triggered_by=triggered_by,
    )

    try:
        send_budget_email(
            subject=subject,
            html_body=html_body,
            recipients=recipients,
        )
        logger.info(
            "Monthly projection job: email sent to %d recipient(s) for month=%s",
            len(recipients), target_month,
        )
    except Exception as exc:
        logger.error(
            "Monthly projection job: failed to send email month=%s: %s",
            target_month, exc,
        )


# ── Standalone scheduler (for testing outside FastAPI) ────────────────────────

def create_projection_scheduler() -> BlockingScheduler:
    scheduler = BlockingScheduler(timezone=_MT)
    scheduler.add_job(
        run_monthly_projection_job,
        CronTrigger(
            day=23,
            hour=8,
            minute=0,
            second=0,
            timezone=_MT,
        ),
        id="monthly_projection_job",
        replace_existing=True,
    )
    logger.info("Projection scheduler created — job runs on 23rd of each month at 08:00 MT")
    return scheduler


def main() -> None:
    import logging as _logging
    _logging.basicConfig(level=_logging.INFO)
    logger.info("Starting monthly projection scheduler")
    create_projection_scheduler().start()


if __name__ == "__main__":
    main()


# ──────────────────────────────────────────────────────────────────────────────
# HOW TO REGISTER THIS JOB IN scheduler/background_scheduler.py
# ──────────────────────────────────────────────────────────────────────────────
#
# 1. Add this import near the top of background_scheduler.py:
#
#       from scheduler.monthly_projection_job import run_monthly_projection_job
#
# 2. Inside start_background_scheduler(), after the existing scheduler.add_job()
#    calls, add:
#
#       # Monthly projection job — runs on the 23rd of every month at 08:00 UTC
#       scheduler.add_job(
#           func=run_monthly_projection_job,
#           trigger=CronTrigger(day=23, hour=8, minute=0),
#           id="monthly_projection_job",
#           name="Monthly Projection Calculation",
#           replace_existing=True,
#       )
#
# That's the only change needed in background_scheduler.py.
# ──────────────────────────────────────────────────────────────────────────────