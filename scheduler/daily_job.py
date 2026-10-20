"""
Daily time log verification report — runs at 1:00 PM US Mountain Time.

Key design decisions:
  - Reads timecard results from the DB cache (populated by unified_cron_job.py
    which syncs from HCSS every 8 hours). No direct HCSS call here.
  - Recipients come from NOTIFY_SUPERVISOR_EMAIL, NOTIFY_PM_EMAIL,
    NOTIFY_ADMIN_EMAIL (same pattern as weekly_quantity_job).
  - After a successful send, a short confirmation is sent to NOTIFY_TEST_EMAIL.
  - If the DB has no data for yesterday (unified cron failed), a "no data"
    alert is sent to NOTIFY_TEST_EMAIL and no team email is sent.
"""
from __future__ import annotations

import logging
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

# Ensure repo root is on path when run directly
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from notifications.email_template import build_email_body
from notifications.individual_email_sender import (
    get_all_notify_recipients,
    send_individual_email,
    send_no_data_alert,
    send_test_confirmation,
)

logger = logging.getLogger(__name__)

_MT = ZoneInfo("America/Denver")

_JOB_NAME = "Daily Time Log Report"


# ---------------------------------------------------------------------------
# Date helper
# ---------------------------------------------------------------------------

def _report_date() -> str:
    """Yesterday's date in US Mountain Time as YYYY-MM-DD."""
    return (datetime.now(_MT).date() - timedelta(days=1)).isoformat()


# ---------------------------------------------------------------------------
# DB cache reader
# ---------------------------------------------------------------------------

def _load_results_from_db(target_date: str):
    """
    Read timecard verification results from the database cache.
    The unified_cron_job populates this cache every 8 hours from HCSS.

    Returns:
        list[TimecardResult] — may be empty if no data was cached.
    """
    # Import here to avoid circular imports at module load time
    from api.cache_service import get_cached_verification
    from api.database import SessionLocal

    db = SessionLocal()
    try:
        cached = get_cached_verification(db, target_date, business_unit_id=None)
        if cached is None:
            logger.warning(
                "DB cache miss for date=%s — unified cron may not have run yet",
                target_date,
            )
            return None
        _, results = cached
        logger.info(
            "Loaded %d timecard result(s) from DB cache for date=%s",
            len(results),
            target_date,
        )
        return results
    except Exception as exc:
        logger.exception("Failed to read DB cache for date=%s: %s", target_date, exc)
        return None
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Main job function  (called by APScheduler + background_scheduler.py)
# ---------------------------------------------------------------------------

def run_daily_time_log_report() -> None:
    """
    1. Determine yesterday's date.
    2. Load results from DB cache (already carrying portal_sync_status set by the
       earlier 12:30 PM run_daily_portal_sync job).
    3. If no data → send no-data alert to test address, stop.
    4. Build the HTML email from the cached results.
    5. Send to all NOTIFY_* recipients.
    6. Send a short confirmation to NOTIFY_TEST_EMAIL.

    Portal Approve/Reject is intentionally NOT done here — see run_daily_portal_sync.
    """
    target_date = _report_date()
    logger.info("Daily time log report job started for date=%s", target_date)

    # ── Step 1: Load from DB ──────────────────────────────────────────────
    results = _load_results_from_db(target_date)

    if results is None:
        # Cache miss — unified cron didn't run or failed
        reason = (
            f"No data found in database for {target_date}. "
            "The unified sync job (runs every 8h) may have failed or not run yet. "
            "Check server logs for errors from scheduler/unified_cron_job.py."
        )
        logger.error("Daily report aborted: %s", reason)
        send_no_data_alert(
            job_name=_JOB_NAME,
            report_date=target_date,
            reason=reason,
        )
        return

    if len(results) == 0:
        # Cache exists but is empty — no timecards that day (weekend / holiday)
        reason = (
            f"DB cache exists for {target_date} but contains 0 timecards. "
            "This is normal for weekends or holidays. No report email sent."
        )
        logger.info("Daily report: zero timecards for %s — skipping team email", target_date)
        send_no_data_alert(
            job_name=_JOB_NAME,
            report_date=target_date,
            reason=reason,
        )
        return

    # NOTE: portal Approve/Reject is NOT done here. It runs as a separate,
    # earlier job (run_daily_portal_sync, ~12:30 PM MT) so the slow browser
    # automation never delays this 1 PM email. By the time this email is sent,
    # each card already carries its portal_sync_status from that job.

    # ── Step 2: Build email ───────────────────────────────────────────────
    subject, html_body = build_email_body(results, target_date)

    rejected = sum(1 for r in results if r.status == "REJECTED")
    flagged  = sum(1 for r in results if r.status == "FLAGGED")
    approved = sum(1 for r in results if r.status == "APPROVED")

    logger.info(
        "Daily report built: date=%s total=%d rejected=%d flagged=%d approved=%d",
        target_date, len(results), rejected, flagged, approved,
    )

    # ── Step 3: Get recipients ────────────────────────────────────────────
    recipients = get_all_notify_recipients()
    if not recipients:
        logger.error(
            "No recipients configured — set NOTIFY_SUPERVISOR_EMAIL, "
            "NOTIFY_PM_EMAIL, or NOTIFY_ADMIN_EMAIL in .env"
        )
        send_no_data_alert(
            job_name=_JOB_NAME,
            report_date=target_date,
            reason=(
                "Report was ready but no recipients are configured. "
                "Set NOTIFY_SUPERVISOR_EMAIL / NOTIFY_PM_EMAIL / NOTIFY_ADMIN_EMAIL in .env."
            ),
        )
        return

    # ── Step 4: Send team email ───────────────────────────────────────────
    try:
        send_individual_email(
            subject=subject,
            html_body=html_body,
            recipients=recipients,
        )
        logger.info(
            "Daily report sent to %d recipient(s): %s",
            len(recipients),
            ", ".join(recipients),
        )
    except Exception as exc:
        logger.exception(
            "Failed to send daily report for date=%s: %s", target_date, exc
        )
        send_no_data_alert(
            job_name=_JOB_NAME,
            report_date=target_date,
            reason=f"Email send failed (SMTP error): {exc}",
        )
        return

    # ── Step 5: Send test confirmation ────────────────────────────────────
    send_test_confirmation(
        job_name=_JOB_NAME,
        report_date=target_date,
        recipients_count=len(recipients),
        extra_detail=(
            f"Summary — Rejected: {rejected} | Flagged: {flagged} | Approved: {approved} "
            f"| Total: {len(results)}"
        ),
    )


async def run_daily_portal_sync() -> None:
    """
    Browser automation job — runs ~12:30 PM MT, BEFORE the 1 PM email, so the
    slow portal work never delays the report.

    For yesterday's cached timecards it Approves/Rejects each one on HeavyJob
    (APPROVED/FLAGGED -> Approve, REJECTED -> Reject; matched by job_code +
    foreman, with auto-retry per card), then writes each card's outcome back to
    the DB as `portal_sync_status` so the 1 PM email and the dashboard reflect
    whether each record is synced with HeavyJob.

    Uses Playwright async API so it can run safely under gunicorn's event loop
    (via a dedicated asyncio.run / thread wrapper in background_scheduler).

    Gated by PORTAL_AUTOMATION_ENABLED; honors PORTAL_AUTOMATION_DRY_RUN (a
    dry-run performs no clicks and leaves statuses unchanged).
    """
    if os.getenv("PORTAL_AUTOMATION_ENABLED", "false").strip().lower() != "true":
        logger.info("Portal sync disabled (set PORTAL_AUTOMATION_ENABLED=true to enable)")
        return

    target_date = _report_date()
    logger.info("Portal sync job started for date=%s", target_date)

    results = _load_results_from_db(target_date)
    if not results:
        logger.warning(
            "Portal sync: no cached timecards for %s (unified sync may not have run) — skipping",
            target_date,
        )
        return

    try:
        from payroll_automation import run_portal_actions

        actions = await run_portal_actions(results, target_date)
    except Exception as exc:
        logger.exception("Portal sync failed for date=%s: %s", target_date, exc)
        return

    # Persist each card's outcome so the email + dashboard show the sync state
    try:
        from api.database import SessionLocal
        from api.db_services import update_portal_sync_status

        db = SessionLocal()
        try:
            updated = update_portal_sync_status(db, target_date, None, actions)
            logger.info("Portal sync complete: %d action(s), %d row(s) marked synced",
                        len(actions), updated)
        finally:
            db.close()
    except Exception as exc:
        logger.exception("Portal sync: failed to write sync status for date=%s: %s", target_date, exc)


# ---------------------------------------------------------------------------
# Standalone scheduler — only used when running daily_job.py directly.
# When running via uvicorn, background_scheduler.py adds this job instead.
# ---------------------------------------------------------------------------

def create_scheduler() -> BlockingScheduler:
    scheduler = BlockingScheduler(timezone=_MT)
    scheduler.add_job(
        run_daily_time_log_report,
        CronTrigger(hour=13, minute=0, second=0, timezone=_MT),
        id="daily_time_log_report",
        replace_existing=True,
    )
    logger.info("Standalone scheduler created — daily job fires at 13:00 MT")
    return scheduler


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    logger.info("Starting Gould APM Daily Report Scheduler (standalone)")
    create_scheduler().start()


if __name__ == "__main__":
    main()