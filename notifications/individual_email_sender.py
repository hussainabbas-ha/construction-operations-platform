"""
Individual timecard email sender.

Sends an HTML email to user-selected recipients for a single timecard.
Uses the same SMTP settings as the daily report sender (SMTP_HOST, SMTP_PORT, etc.)
but allows dynamic recipients chosen from the frontend modal.

Hardcoded recipient options are loaded from .env:
  NOTIFY_SUPERVISOR_EMAIL   — Direct Supervisor (UI checkbox + scheduled)
  NOTIFY_PM_EMAIL           — Project Manager (UI checkbox + scheduled)
  NOTIFY_ADMIN_EMAIL        — Admin Team (UI checkbox + scheduled)
  NOTIFY_EXTRA_EMAILS       — Optional comma-separated extras for scheduled
                              jobs only (not shown in the UI role directory)

Shared helpers used by ALL scheduled jobs:
  get_all_notify_recipients()  — role emails + NOTIFY_EXTRA_EMAILS, deduped
  get_test_email()             — returns NOTIFY_TEST_EMAIL or None
  send_test_confirmation()     — sends a short "scheduler fired" email
  send_no_data_alert()         — sends a "no data found" alert to test address
"""
from __future__ import annotations

import logging
import os
import smtplib
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr
from zoneinfo import ZoneInfo

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

logger = logging.getLogger(__name__)

_MT = ZoneInfo("America/Denver")


# ---------------------------------------------------------------------------
# Recipient helpers — used by ALL scheduled jobs
# ---------------------------------------------------------------------------

def get_recipient_directory() -> dict[str, str]:
    """
    Return the hardcoded recipient options as {role: email}.
    Entries with empty/missing env values are excluded.
    Used by the frontend modal to show checkbox options.

    Does NOT include NOTIFY_EXTRA_EMAILS — those are scheduled-job-only
    and would not map cleanly to a single UI role checkbox.
    """
    mapping = {
        "supervisor": os.environ.get("NOTIFY_SUPERVISOR_EMAIL", "").strip(),
        "project_manager": os.environ.get("NOTIFY_PM_EMAIL", "").strip(),
        "admin": os.environ.get("NOTIFY_ADMIN_EMAIL", "").strip(),
    }
    return {k: v for k, v in mapping.items() if v}


def _parse_extra_notify_emails() -> list[str]:
    """
    Split NOTIFY_EXTRA_EMAILS on commas into a clean list.
    Empty / missing env → [].
    """
    raw = os.environ.get("NOTIFY_EXTRA_EMAILS", "").strip()
    if not raw:
        return []
    return [part.strip() for part in raw.split(",") if part.strip()]


def get_all_notify_recipients() -> list[str]:
    """
    Return all scheduled-job recipient emails as a flat deduplicated list.
    This is what ALL scheduled jobs (daily report, weekly quantity, budget)
    use to send to the full team.

    Reads from:
      NOTIFY_SUPERVISOR_EMAIL
      NOTIFY_PM_EMAIL
      NOTIFY_ADMIN_EMAIL
      NOTIFY_EXTRA_EMAILS   (optional, comma-separated)

    Deduping is case-insensitive. Order is role emails first, then extras.
    Returns empty list if none are configured — caller should warn.
    """
    seen: set[str] = set()
    recipients: list[str] = []

    def _add(email: str) -> None:
        email_clean = email.strip()
        if not email_clean:
            return
        key = email_clean.lower()
        if key not in seen:
            seen.add(key)
            recipients.append(email_clean)

    for email in get_recipient_directory().values():
        _add(email)

    for email in _parse_extra_notify_emails():
        _add(email)

    return recipients


def get_test_email() -> str | None:
    """
    Return the NOTIFY_TEST_EMAIL address, or None if not configured.
    Used to send scheduler health-check emails.
    """
    val = os.environ.get("NOTIFY_TEST_EMAIL", "").strip()
    return val if val else None


# ---------------------------------------------------------------------------
# Test / monitor email senders
# ---------------------------------------------------------------------------

def send_test_confirmation(
    *,
    job_name: str,
    report_date: str,
    recipients_count: int,
    extra_detail: str = "",
) -> None:
    """
    Send a short confirmation email to NOTIFY_TEST_EMAIL to confirm
    the scheduler fired and the report email was sent successfully.

    Does nothing if NOTIFY_TEST_EMAIL is not set.
    """
    test_addr = get_test_email()
    if not test_addr:
        return

    now_str = datetime.now(_MT).strftime("%Y-%m-%d %H:%M %Z")
    subject = f"[SCHEDULER OK] {job_name} — {report_date}"

    detail_block = (
        f"<p style='margin:8px 0;font-size:13px;color:#374151;"
        f"font-family:Arial,Helvetica,sans-serif;'>{extra_detail}</p>"
        if extra_detail else ""
    )

    html_body = (
        '<div style="font-family:Arial,Helvetica,sans-serif;color:#111827;max-width:480px;">'
        f'<h2 style="margin:0 0 12px 0;font-size:16px;color:#15803d;">✓ Scheduler Fired Successfully</h2>'
        '<table cellpadding="0" cellspacing="0" border="0" style="width:100%;border-collapse:collapse;">'
        f'<tr><td style="padding:6px 10px;border:1px solid #e5e7eb;font-size:13px;'
        f'font-weight:bold;color:#374151;background:#f9fafb;width:130px;'
        f'font-family:Arial,Helvetica,sans-serif;">Job</td>'
        f'<td style="padding:6px 10px;border:1px solid #e5e7eb;font-size:13px;'
        f'font-family:Arial,Helvetica,sans-serif;">{job_name}</td></tr>'
        f'<tr><td style="padding:6px 10px;border:1px solid #e5e7eb;font-size:13px;'
        f'font-weight:bold;color:#374151;background:#f9fafb;'
        f'font-family:Arial,Helvetica,sans-serif;">Report Date</td>'
        f'<td style="padding:6px 10px;border:1px solid #e5e7eb;font-size:13px;'
        f'font-family:Arial,Helvetica,sans-serif;">{report_date}</td></tr>'
        f'<tr><td style="padding:6px 10px;border:1px solid #e5e7eb;font-size:13px;'
        f'font-weight:bold;color:#374151;background:#f9fafb;'
        f'font-family:Arial,Helvetica,sans-serif;">Recipients</td>'
        f'<td style="padding:6px 10px;border:1px solid #e5e7eb;font-size:13px;'
        f'font-family:Arial,Helvetica,sans-serif;">{recipients_count} email(s) sent</td></tr>'
        f'<tr><td style="padding:6px 10px;border:1px solid #e5e7eb;font-size:13px;'
        f'font-weight:bold;color:#374151;background:#f9fafb;'
        f'font-family:Arial,Helvetica,sans-serif;">Fired At</td>'
        f'<td style="padding:6px 10px;border:1px solid #e5e7eb;font-size:13px;'
        f'font-family:Arial,Helvetica,sans-serif;">{now_str}</td></tr>'
        "</table>"
        + detail_block
        + '<p style="margin:20px 0 0 0;padding-top:12px;border-top:1px solid #e5e7eb;'
        'font-size:11px;color:#9ca3af;font-family:Arial,Helvetica,sans-serif;">'
        "Construction Operations API — Scheduler Monitor"
        "</p></div>"
    )

    try:
        send_individual_email(subject=subject, html_body=html_body, recipients=[test_addr])
        logger.info("Test confirmation sent to %s for job=%s date=%s", test_addr, job_name, report_date)
    except Exception as exc:
        logger.error("Failed to send test confirmation to %s: %s", test_addr, exc)


def send_no_data_alert(
    *,
    job_name: str,
    report_date: str,
    reason: str,
) -> None:
    """
    Send an alert email to NOTIFY_TEST_EMAIL when a scheduled job
    found no data in the DB (e.g. unified cron job failed to fetch
    from HCSS, so DB cache is empty for that date).

    Does nothing if NOTIFY_TEST_EMAIL is not set.
    """
    test_addr = get_test_email()
    if not test_addr:
        return

    now_str = datetime.now(_MT).strftime("%Y-%m-%d %H:%M %Z")
    subject = f"[SCHEDULER WARNING] {job_name} — No Data for {report_date}"

    html_body = (
        '<div style="font-family:Arial,Helvetica,sans-serif;color:#111827;max-width:480px;">'
        f'<h2 style="margin:0 0 12px 0;font-size:16px;color:#b91c1c;">⚠ No Data Available</h2>'
        '<p style="margin:0 0 12px 0;font-size:13px;color:#374151;'
        'font-family:Arial,Helvetica,sans-serif;">'
        "A scheduled job ran but found no data in the database for the target date. "
        "The report email was <strong>not sent</strong> to the team. "
        "This usually means the background sync job (unified cron) failed to fetch "
        "data from HCSS. Please check the server logs."
        "</p>"
        '<table cellpadding="0" cellspacing="0" border="0" style="width:100%;border-collapse:collapse;">'
        f'<tr><td style="padding:6px 10px;border:1px solid #e5e7eb;font-size:13px;'
        f'font-weight:bold;color:#374151;background:#fde8e8;width:130px;'
        f'font-family:Arial,Helvetica,sans-serif;">Job</td>'
        f'<td style="padding:6px 10px;border:1px solid #e5e7eb;font-size:13px;'
        f'font-family:Arial,Helvetica,sans-serif;">{job_name}</td></tr>'
        f'<tr><td style="padding:6px 10px;border:1px solid #e5e7eb;font-size:13px;'
        f'font-weight:bold;color:#374151;background:#fde8e8;'
        f'font-family:Arial,Helvetica,sans-serif;">Report Date</td>'
        f'<td style="padding:6px 10px;border:1px solid #e5e7eb;font-size:13px;'
        f'font-family:Arial,Helvetica,sans-serif;">{report_date}</td></tr>'
        f'<tr><td style="padding:6px 10px;border:1px solid #e5e7eb;font-size:13px;'
        f'font-weight:bold;color:#374151;background:#fde8e8;'
        f'font-family:Arial,Helvetica,sans-serif;">Reason</td>'
        f'<td style="padding:6px 10px;border:1px solid #e5e7eb;font-size:13px;color:#b91c1c;'
        f'font-family:Arial,Helvetica,sans-serif;">{reason}</td></tr>'
        f'<tr><td style="padding:6px 10px;border:1px solid #e5e7eb;font-size:13px;'
        f'font-weight:bold;color:#374151;background:#fde8e8;'
        f'font-family:Arial,Helvetica,sans-serif;">Detected At</td>'
        f'<td style="padding:6px 10px;border:1px solid #e5e7eb;font-size:13px;'
        f'font-family:Arial,Helvetica,sans-serif;">{now_str}</td></tr>'
        "</table>"
        '<p style="margin:20px 0 0 0;padding-top:12px;border-top:1px solid #e5e7eb;'
        'font-size:11px;color:#9ca3af;font-family:Arial,Helvetica,sans-serif;">'
        "Construction Operations API — Scheduler Monitor"
        "</p></div>"
    )

    try:
        send_individual_email(subject=subject, html_body=html_body, recipients=[test_addr])
        logger.warning(
            "No-data alert sent to %s for job=%s date=%s reason=%s",
            test_addr, job_name, report_date, reason,
        )
    except Exception as exc:
        logger.error("Failed to send no-data alert to %s: %s", test_addr, exc)


# ---------------------------------------------------------------------------
# Core email sender (unchanged)
# ---------------------------------------------------------------------------

#def send_individual_email(
#    *,
#    subject: str,
#    html_body: str,
#    recipients: list[str],
#) -> None:
#    """
#    Send an HTML email to the given list of recipient addresses.
#
#    Uses the same SMTP_HOST / SMTP_PORT / SMTP_USERNAME / SMTP_PASSWORD
#    settings as the daily report sender.
#
#    Raises ValueError if SMTP settings are missing.
#    Raises smtplib errors on delivery failure.
#    """
#    host = os.environ.get("SMTP_HOST", "").strip()
#    port_str = os.environ.get("SMTP_PORT", "587").strip()
#    username = os.environ.get("SMTP_USERNAME", "").strip()
#    password = os.environ.get("SMTP_PASSWORD", "")
#    from_name = os.environ.get("SMTP_FROM_NAME", "Gould APM Bot").strip()
#
#    if not host:
#        raise ValueError("SMTP_HOST is not set")
#    if not username:
#        raise ValueError("SMTP_USERNAME is not set")
#    if not recipients:
#        raise ValueError("No recipients provided")
#
#    try:
#        port = int(port_str)
#    except ValueError as e:
#        raise ValueError(f"SMTP_PORT must be an integer, got {port_str!r}") from e
#
#    logger.info(
#        "Sending email to %d recipient(s): %s",
#        len(recipients),
#        ", ".join(recipients),
#    )
#
#    msg = MIMEMultipart("alternative")
#    msg["Subject"] = subject
#    msg["From"] = formataddr((from_name, username))
#    msg["To"] = ", ".join(recipients)
#    msg.attach(MIMEText(html_body, "html", "utf-8"))
#
#    with smtplib.SMTP(host, port, timeout=60) as server:
#        server.starttls()
#        if password:
#            server.login(username, password)
#        server.sendmail(username, recipients, msg.as_string())
#
#    logger.info("Email sent successfully to %d recipient(s)", len(recipients))


def send_individual_email(
    *,
    subject: str,
    html_body: str,
    recipients: list[str],
) -> None:
    """Send individual email via Microsoft Graph API."""
    from notifications.graph_email_sender import send_graph_email

    if not recipients:
        raise ValueError("No recipients provided")

    logger.info(
        "Sending email to %d recipient(s): %s",
        len(recipients), ", ".join(recipients),
    )
    send_graph_email(subject=subject, html_body=html_body, recipients=recipients)