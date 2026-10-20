"""
Individual timecard email template.

Builds an HTML email for a single timecard row triggered by the
per-row "Send Email" button in the frontend.

Includes:
  - Timecard details (ID, date, job, foreman, status)
  - AI Analysis (verification reasons / flags)
  - User comments (optional)
"""
from __future__ import annotations

import html
from datetime import datetime
from zoneinfo import ZoneInfo

_MT = ZoneInfo("America/Denver")


def _status_colour(status: str) -> tuple[str, str]:
    """Return (background, text colour) for the status badge."""
    colours = {
        "REJECTED": ("#fde8e8", "#b91c1c"),
        "FLAGGED": ("#ffedd5", "#c2410c"),
        "APPROVED": ("#dcfce7", "#15803d"),
    }
    return colours.get(status.upper(), ("#f3f4f6", "#374151"))


def build_individual_email_body(
    *,
    timecard_id: str,
    date: str,
    job_code: str,
    foreman: str,
    status: str,
    reasons: list[str],
    flags: list[str],
    comments: str = "",
) -> tuple[str, str]:
    """
    Build subject + HTML body for a single timecard notification.

    Returns:
        (subject, html_body)
    """
    # --- Subject ---
    subject = (
        f"Time Log Verification Issue — {html.escape(job_code)} | "
        f"{html.escape(foreman)} | {date} [{status.upper()}]"
    )

    now_str = datetime.now(_MT).strftime("%Y-%m-%d %H:%M %Z")
    bg, fg = _status_colour(status)

    # --- AI Analysis section ---
    ai_lines: list[str] = []
    if reasons:
        for r in reasons:
            ai_lines.append(
                f'<li style="margin:4px 0;font-size:14px;color:#b91c1c;">'
                f'{html.escape(r)}</li>'
            )
    if flags:
        for f in flags:
            ai_lines.append(
                f'<li style="margin:4px 0;font-size:14px;color:#c2410c;">'
                f'{html.escape(f)}</li>'
            )
    if not ai_lines:
        ai_analysis_html = (
            '<p style="margin:8px 0;font-size:14px;color:#15803d;'
            'font-family:Arial,Helvetica,sans-serif;">No issues detected.</p>'
        )
    else:
        ai_analysis_html = (
            '<ul style="margin:8px 0 8px 20px;padding:0;'
            'font-family:Arial,Helvetica,sans-serif;">'
            + "".join(ai_lines)
            + "</ul>"
        )

    # --- Comments section (only if user typed something) ---
    comments_html = ""
    if comments and comments.strip():
        comments_html = (
            '<tr><td colspan="2" style="padding:16px 0 0 0;">'
            '<h3 style="margin:0 0 8px 0;font-size:14px;color:#374151;'
            'font-family:Arial,Helvetica,sans-serif;">Comments</h3>'
            '<div style="padding:12px 16px;background:#f9fafb;border:1px solid #e5e7eb;'
            'border-radius:6px;font-size:14px;line-height:1.5;color:#1f2937;'
            f'font-family:Arial,Helvetica,sans-serif;">{html.escape(comments)}</div>'
            "</td></tr>"
        )

    # --- Full HTML body ---
    html_body = (
        '<div style="font-family:Arial,Helvetica,sans-serif;color:#111827;'
        'max-width:640px;margin:0 auto;">'
        # Header
        '<h2 style="margin:0 0 16px 0;font-size:18px;color:#111827;">'
        "Time Log Verification — Individual Timecard Report</h2>"
        # Details table
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
        'style="width:100%;max-width:640px;border-collapse:collapse;margin-bottom:16px;">'
        # Row: ID
        "<tr>"
        '<td style="padding:8px 12px;border:1px solid #e5e7eb;font-size:13px;'
        'font-weight:bold;color:#374151;background:#f9fafb;width:140px;'
        'font-family:Arial,Helvetica,sans-serif;">Record ID</td>'
        '<td style="padding:8px 12px;border:1px solid #e5e7eb;font-size:13px;'
        f'font-family:Arial,Helvetica,sans-serif;">{html.escape(timecard_id)}</td>'
        "</tr>"
        # Row: Date
        "<tr>"
        '<td style="padding:8px 12px;border:1px solid #e5e7eb;font-size:13px;'
        'font-weight:bold;color:#374151;background:#f9fafb;'
        'font-family:Arial,Helvetica,sans-serif;">Date</td>'
        '<td style="padding:8px 12px;border:1px solid #e5e7eb;font-size:13px;'
        f'font-family:Arial,Helvetica,sans-serif;">{html.escape(date)}</td>'
        "</tr>"
        # Row: Job
        "<tr>"
        '<td style="padding:8px 12px;border:1px solid #e5e7eb;font-size:13px;'
        'font-weight:bold;color:#374151;background:#f9fafb;'
        'font-family:Arial,Helvetica,sans-serif;">Job</td>'
        '<td style="padding:8px 12px;border:1px solid #e5e7eb;font-size:13px;'
        f'font-family:Arial,Helvetica,sans-serif;">{html.escape(job_code)}</td>'
        "</tr>"
        # Row: Foreman
        "<tr>"
        '<td style="padding:8px 12px;border:1px solid #e5e7eb;font-size:13px;'
        'font-weight:bold;color:#374151;background:#f9fafb;'
        'font-family:Arial,Helvetica,sans-serif;">Foreman</td>'
        '<td style="padding:8px 12px;border:1px solid #e5e7eb;font-size:13px;'
        f'font-family:Arial,Helvetica,sans-serif;">{html.escape(foreman)}</td>'
        "</tr>"
        # Row: Status (coloured badge)
        "<tr>"
        '<td style="padding:8px 12px;border:1px solid #e5e7eb;font-size:13px;'
        'font-weight:bold;color:#374151;background:#f9fafb;'
        'font-family:Arial,Helvetica,sans-serif;">Status</td>'
        '<td style="padding:8px 12px;border:1px solid #e5e7eb;font-size:13px;'
        'font-family:Arial,Helvetica,sans-serif;">'
        f'<span style="display:inline-block;padding:4px 12px;border-radius:4px;'
        f'background:{bg};color:{fg};font-weight:bold;font-size:12px;">'
        f"{html.escape(status.upper())}</span></td>"
        "</tr>"
        # Row: AI Analysis
        "<tr>"
        '<td colspan="2" style="padding:16px 0 0 0;">'
        '<h3 style="margin:0 0 8px 0;font-size:14px;color:#374151;'
        'font-family:Arial,Helvetica,sans-serif;">AI Analysis</h3>'
        + ai_analysis_html
        + "</td></tr>"
        # Row: Comments (conditional)
        + comments_html
        + "</table>"
        # Footer
        '<p style="margin:24px 0 0 0;padding-top:16px;border-top:1px solid #e5e7eb;'
        'font-size:12px;color:#6b7280;font-family:Arial,Helvetica,sans-serif;">'
        f"Sent by Construction Operations API — {html.escape(now_str)}"
        "</p>"
        "</div>"
    )

    return subject, html_body