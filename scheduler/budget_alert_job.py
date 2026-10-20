"""
Budget Alert Scheduler Jobs — scheduler/budget_alert_job.py

Two budget monitoring jobs that run automatically inside the FastAPI process:

  Job 1 — run_daily_budget_alert()
    Fires:   Every day at 2:00 PM US Mountain Time
    Purpose: Loads yesterday's budget via the same helpers as the dashboard
             (/api/budget), finds OVER_RISK items, sends alert email to team
             if any found. If nothing at risk → silent skip (test confirmation only).

  Job 2 — run_weekly_budget_summary()
    Fires:   Every Monday at 8:00 AM US Mountain Time
    Purpose: Aggregates last 7 days of budget data via the same helpers as the
             dashboard, sends full summary (ON_TRACK + OVER_RISK) to the team.

expected_budget (Expected All Cost) is computed on the fly the same way as the
dashboard / POST /api/budget/notify / test_all_emails.py:

    expected_budget = min(
        (installed_qty / planned_qty) * budgeted_all_cost,
        budgeted_all_cost,
    )

using quantity tracking data for the same period. It is NOT read from the DB
(the DB only stores budgeted_all_cost + actual_cost).

Email uses existing notifications/budget_email_sender.py + budget_email_template.py.
"""
from __future__ import annotations

import logging
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

# ── Ensure repo root is on path when run directly ────────────────────────────
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from notifications.budget_email_sender import send_budget_email
from notifications.budget_email_template import build_budget_email_body
from notifications.individual_email_sender import (
    get_all_notify_recipients,
    send_no_data_alert,
    send_test_confirmation,
)

logger = logging.getLogger(__name__)

_MT = ZoneInfo("America/Denver")

_DAILY_JOB_NAME  = "Daily Budget Alert"
_WEEKLY_JOB_NAME = "Weekly Budget Summary"


# ============================================================================
# INTERNAL HELPERS
# ============================================================================

def _report_date() -> str:
    """Yesterday's date in US Mountain Time as YYYY-MM-DD."""
    return (datetime.now(_MT).date() - timedelta(days=1)).isoformat()


def _fetch_budget_items(start_date: str, end_date: str, *, request_id: str):
    """
    Load + aggregate budget rows using the SAME helpers as the dashboard.

    Returns (items, qty_lookup) where each item already has expected_budget,
    utilization, variance, loss_*, and status computed.

    Returns (None, None) when no budget data is available for the period
    (mirrors the API's 404 "no data" case without raising into the scheduler).
    """
    from fastapi import HTTPException

    from api.routes.budget import _fetch_for_range

    try:
        return _fetch_for_range(
            start_date,
            end_date,
            request_id=request_id,
        )
    except HTTPException as exc:
        if exc.status_code == 404:
            logger.warning(
                "Budget fetch: no data for %s→%s (%s)",
                start_date, end_date, exc.detail,
            )
            return None, None
        raise


# ============================================================================
# JOB 1 — DAILY BUDGET ALERT (2:00 PM MT)
# ============================================================================

def run_daily_budget_alert() -> None:
    """
    Reads yesterday's budget (dashboard metrics), sends an alert email to the
    team if any cost codes are OVER_RISK.

    Flow:
        1. Determine yesterday's date (Mountain Time).
        2. Load via _fetch_for_range (same as dashboard) — abort if no data.
        3. Filter OVER_RISK items only.
        4. If nothing at risk → log it + send test confirmation, no team email.
        5. Get NOTIFY_* recipients from .env.
        6. Build email using existing budget template.
        7. Send to team via existing budget email sender.
        8. Send test confirmation to NOTIFY_TEST_EMAIL.
    """
    from api.routes.budget import _to_budget_item_row

    target_date = _report_date()
    logger.info("[%s] Job started for date=%s", _DAILY_JOB_NAME, target_date)

    # ── Step 1: Load via dashboard helpers (computes expected_budget) ────
    results, qty_lookup = _fetch_budget_items(
        target_date,
        target_date,
        request_id="daily-budget-alert",
    )

    if results is None:
        reason = (
            f"No budget data in database for {target_date}. "
            "The unified sync job (runs every 8h) may have failed or not run yet. "
            "Check server logs for errors from scheduler/unified_cron_job.py."
        )
        logger.error("[%s] Aborted — %s", _DAILY_JOB_NAME, reason)
        send_no_data_alert(
            job_name=_DAILY_JOB_NAME,
            report_date=target_date,
            reason=reason,
        )
        return

    if len(results) == 0:
        reason = (
            f"Budget fetch for {target_date} returned 0 cost codes. "
            "No active jobs with budget data on this date. "
            "This may be normal for weekends or holidays."
        )
        logger.info("[%s] Zero items for %s — skipping", _DAILY_JOB_NAME, target_date)
        send_no_data_alert(
            job_name=_DAILY_JOB_NAME,
            report_date=target_date,
            reason=reason,
        )
        return

    # ── Step 2: Filter OVER_RISK only (status from dashboard metrics) ───
    over_risk = [r for r in results if r.get("status") == "OVER_RISK"]
    on_track  = [r for r in results if r.get("status") == "ON_TRACK"]

    logger.info(
        "[%s] date=%s total=%d over_risk=%d on_track=%d",
        _DAILY_JOB_NAME, target_date, len(results), len(over_risk), len(on_track),
    )

    # ── Step 3: Skip if nothing at risk ─────────────────────────────────
    if not over_risk:
        logger.info(
            "[%s] All %d cost codes ON_TRACK for %s — no team email sent",
            _DAILY_JOB_NAME, len(results), target_date,
        )
        send_test_confirmation(
            job_name=_DAILY_JOB_NAME,
            report_date=target_date,
            recipients_count=0,
            extra_detail=(
                f"All {len(results)} cost code(s) are ON_TRACK. "
                "No alert email was sent to the team."
            ),
        )
        return

    # ── Step 4: Get recipients ───────────────────────────────────────────
    recipients = get_all_notify_recipients()
    if not recipients:
        logger.error("[%s] No recipients configured", _DAILY_JOB_NAME)
        send_no_data_alert(
            job_name=_DAILY_JOB_NAME,
            report_date=target_date,
            reason=(
                f"{len(over_risk)} OVER_RISK item(s) found but no recipients configured. "
                "Set NOTIFY_SUPERVISOR_EMAIL / NOTIFY_PM_EMAIL / NOTIFY_ADMIN_EMAIL in .env."
            ),
        )
        return

    # ── Step 5: Build email (same row shape as dashboard notify) ─────────
    email_items = [_to_budget_item_row(r, qty_lookup) for r in over_risk]
    subject, html_body = build_budget_email_body(
        period_start=target_date,
        period_end=target_date,
        items=email_items,
        comments=(
            f"DAILY ALERT — {len(over_risk)} cost code(s) have reached "
            "budget risk level. Immediate review required."
        ),
    )
    # Override subject for clear alert identification
    subject = (
        f"Budget Alert — {len(over_risk)} OVER RISK item(s) require review | {target_date}"
    )

    # ── Step 6: Send to team ─────────────────────────────────────────────
    try:
        send_budget_email(
            subject=subject,
            html_body=html_body,
            recipients=recipients,
        )
        logger.info(
            "[%s] Alert sent: %d over-risk items to %d recipient(s)",
            _DAILY_JOB_NAME, len(over_risk), len(recipients),
        )
    except Exception as exc:
        logger.exception(
            "[%s] Failed to send email for date=%s: %s",
            _DAILY_JOB_NAME, target_date, exc,
        )
        send_no_data_alert(
            job_name=_DAILY_JOB_NAME,
            report_date=target_date,
            reason=f"Email send failed (SMTP error): {exc}",
        )
        return

    # ── Step 7: Test confirmation ────────────────────────────────────────
    send_test_confirmation(
        job_name=_DAILY_JOB_NAME,
        report_date=target_date,
        recipients_count=len(recipients),
        extra_detail=(
            f"Over Risk: {len(over_risk)} | On Track: {len(on_track)} "
            f"| Total Cost Codes: {len(results)}"
        ),
    )


# ============================================================================
# JOB 2 — WEEKLY BUDGET SUMMARY (Monday 8:00 AM MT)
# ============================================================================

def run_weekly_budget_summary() -> None:
    """
    Aggregates budget data across the past 7 days (dashboard metrics), sends a
    full summary email to the team every Monday (ON_TRACK + OVER_RISK both included).

    Flow:
        1. Compute date range: last 7 days.
        2. Load via _fetch_for_range (same as dashboard) — abort if no data.
        3. Get NOTIFY_* recipients.
        4. Build full summary email using existing budget template.
        5. Send to team.
        6. Send test confirmation.
    """
    from api.routes.budget import _to_budget_item_row

    today      = date.today()
    end_date   = (today - timedelta(days=1)).isoformat()   # yesterday
    start_date = (today - timedelta(days=7)).isoformat()   # 7 days back

    logger.info(
        "[%s] Job started: period=%s to %s",
        _WEEKLY_JOB_NAME, start_date, end_date,
    )

    # ── Step 1: Load via dashboard helpers (computes expected_budget) ────
    aggregated, qty_lookup = _fetch_budget_items(
        start_date,
        end_date,
        request_id="weekly-budget-summary",
    )

    if not aggregated:
        reason = (
            f"No budget data found in database for period {start_date} to {end_date}. "
            "The unified sync job may have failed for all dates in this window. "
            "Check server logs for errors from scheduler/unified_cron_job.py."
        )
        logger.error("[%s] Aborted — %s", _WEEKLY_JOB_NAME, reason)
        send_no_data_alert(
            job_name=_WEEKLY_JOB_NAME,
            report_date=f"{start_date} to {end_date}",
            reason=reason,
        )
        return

    over_risk_count = sum(1 for r in aggregated if r.get("status") == "OVER_RISK")
    on_track_count  = sum(1 for r in aggregated if r.get("status") == "ON_TRACK")

    logger.info(
        "[%s] Aggregated: total=%d over_risk=%d on_track=%d",
        _WEEKLY_JOB_NAME, len(aggregated), over_risk_count, on_track_count,
    )

    # ── Step 2: Get recipients ───────────────────────────────────────────
    recipients = get_all_notify_recipients()
    if not recipients:
        logger.error("[%s] No recipients configured", _WEEKLY_JOB_NAME)
        send_no_data_alert(
            job_name=_WEEKLY_JOB_NAME,
            report_date=f"{start_date} to {end_date}",
            reason=(
                "Weekly budget summary ready but no recipients configured. "
                "Set NOTIFY_SUPERVISOR_EMAIL / NOTIFY_PM_EMAIL / NOTIFY_ADMIN_EMAIL in .env."
            ),
        )
        return

    # ── Step 3: Build email (same row shape as dashboard notify) ─────────
    email_items = [_to_budget_item_row(r, qty_lookup) for r in aggregated]
    subject, html_body = build_budget_email_body(
        period_start=start_date,
        period_end=end_date,
        items=email_items,
        comments="",
    )
    subject = (
        f"Weekly Budget Summary — {over_risk_count} Over Risk, "
        f"{on_track_count} On Track | {start_date} to {end_date}"
    )

    # ── Step 4: Send to team ─────────────────────────────────────────────
    try:
        send_budget_email(
            subject=subject,
            html_body=html_body,
            recipients=recipients,
        )
        logger.info(
            "[%s] Summary sent: %d items to %d recipient(s)",
            _WEEKLY_JOB_NAME, len(aggregated), len(recipients),
        )
    except Exception as exc:
        logger.exception("[%s] Failed to send email: %s", _WEEKLY_JOB_NAME, exc)
        send_no_data_alert(
            job_name=_WEEKLY_JOB_NAME,
            report_date=f"{start_date} to {end_date}",
            reason=f"Email send failed (SMTP error): {exc}",
        )
        return

    # ── Step 5: Test confirmation ────────────────────────────────────────
    send_test_confirmation(
        job_name=_WEEKLY_JOB_NAME,
        report_date=f"{start_date} to {end_date}",
        recipients_count=len(recipients),
        extra_detail=(
            f"Over Risk: {over_risk_count} | On Track: {on_track_count} "
            f"| Total Cost Codes: {len(aggregated)}"
        ),
    )
