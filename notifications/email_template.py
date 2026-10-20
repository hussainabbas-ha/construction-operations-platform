from __future__ import annotations

import html
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from payroll_verification.verifier import TimecardResult

logger = logging.getLogger(__name__)
_MT = ZoneInfo("America/Denver")


def _get(item, key: str, default=None):
    if isinstance(item, dict):
        return item.get(key, default)
    return getattr(item, key, default)


def _format_us_date(value: str) -> str:
    try:
        return datetime.strptime(value, "%Y-%m-%d").strftime("%m-%d-%Y")
    except (TypeError, ValueError):
        return value or ""


def _status_badge(status: str) -> str:
    status = (status or "").upper()

    if status == "APPROVED":
        bg, color, label = "#dcfce7", "#047857", "APPROVED"
    elif status == "FLAGGED":
        bg, color, label = "#ffedd5", "#ea580c", "FLAGGED"
    else:
        bg, color, label = "#dbeafe", "#2563eb", "REJECTED"

    return (
        f'<span style="display:inline-block;background:{bg};color:{color};'
        f'font-size:10px;line-height:14px;font-weight:800;text-transform:uppercase;'
        f'letter-spacing:0.05em;border-radius:999px;padding:5px 10px;white-space:nowrap;">'
        f'{html.escape(label)}</span>'
    )


def _metric_card(
    label: str,
    value: int,
    percent: float,
    icon_color: str,
    padding_right: str = "10px",
) -> str:
    return (
        f'<td style="width:33.33%;padding:0 {padding_right} 0 0;vertical-align:top;">'
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
        'style="width:100%;border-collapse:separate;border-spacing:0;background:#f8fafc;'
        'border:1px solid #f1f5f9;border-radius:18px;overflow:hidden;">'
        "<tr>"
        '<td style="padding:18px 18px 16px 18px;font-family:Arial,Helvetica,sans-serif;">'
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
        'style="width:100%;border-collapse:collapse;">'
        "<tr>"
        f'<td style="font-size:15px;line-height:20px;color:#0f172a;font-weight:700;">'
        f'{html.escape(label)}</td>'
        '<td style="text-align:right;">'
        f'<span style="display:inline-block;width:34px;height:34px;border-radius:12px;'
        f'background:#eef2f7;color:{icon_color};font-size:18px;line-height:34px;'
        f'text-align:center;font-weight:800;">•</span>'
        "</td>"
        "</tr>"
        "</table>"
        f'<div style="margin-top:30px;font-size:38px;line-height:42px;color:#020617;'
        f'font-weight:800;letter-spacing:-0.04em;">{value}'
        f'<span style="font-size:12px;line-height:16px;color:#94a3b8;font-weight:800;'
        f'letter-spacing:0.08em;text-transform:uppercase;margin-left:8px;">'
        f'{percent:.1f}% of total</span></div>'
        "</td>"
        "</tr>"
        "</table>"
        "</td>"
    )


def _action_required_badge(status: str) -> str:
    if status not in ("REJECTED", "FLAGGED"):
        return ""

    return (
        '<br><span style="display:inline-block;background:#eef2f7;color:#475569;'
        'font-size:10px;line-height:13px;font-weight:800;border-radius:999px;'
        'padding:3px 8px;margin-top:5px;white-space:nowrap;">Action Required</span>'
    )


def _reason_text(result: TimecardResult) -> str:
    reasons = _get(result, "reasons", []) or []
    if reasons:
        return " ".join(str(r) for r in reasons)
    return "No issues"


def _build_rows(results: list[TimecardResult]) -> str:
    rows: list[str] = []

    order = {
        "REJECTED": 0,
        "FLAGGED": 1,
        "APPROVED": 2,
    }

    sorted_results = sorted(
        results,
        key=lambda r: (
            order.get(_get(r, "status", ""), 9),
            _get(r, "date", "") or "",
            _get(r, "job_code", "") or "",
            _get(r, "foreman", "") or "",
        ),
    )

    for r in sorted_results:
        date = _format_us_date(_get(r, "date", "") or "")
        job_code = _get(r, "job_code", "") or ""
        job_description = _get(r, "job_description", "") or ""
        foreman = _get(r, "foreman", "") or ""
        status = _get(r, "status", "") or ""
        reason = _reason_text(r)

        reason_html = html.escape(reason) + _action_required_badge(status)

        rows.append(
            "<tr>"
            f'<td style="padding:15px 14px;border-bottom:1px solid #eef2f7;'
            f'font-family:Arial,Helvetica,sans-serif;font-size:12px;line-height:18px;'
            f'color:#475569;white-space:nowrap;">{html.escape(date)}</td>'

            f'<td style="padding:15px 14px;border-bottom:1px solid #eef2f7;'
            f'font-family:Arial,Helvetica,sans-serif;font-size:12px;line-height:18px;'
            f'color:#334155;white-space:nowrap;">{html.escape(job_code)}</td>'

            f'<td style="padding:15px 14px;border-bottom:1px solid #eef2f7;'
            f'font-family:Arial,Helvetica,sans-serif;font-size:12px;line-height:18px;'
            f'color:#334155;font-weight:700;max-width:220px;">'
            f'{html.escape(job_description)}</td>'

            f'<td style="padding:15px 14px;border-bottom:1px solid #eef2f7;'
            f'font-family:Arial,Helvetica,sans-serif;font-size:12px;line-height:18px;'
            f'color:#475569;max-width:145px;">{html.escape(foreman)}</td>'

            f'<td style="padding:15px 14px;border-bottom:1px solid #eef2f7;'
            f'font-family:Arial,Helvetica,sans-serif;font-size:12px;line-height:18px;'
            f'text-align:center;white-space:nowrap;">{_status_badge(status)}</td>'

            f'<td style="padding:15px 14px;border-bottom:1px solid #eef2f7;'
            f'font-family:Arial,Helvetica,sans-serif;font-size:12px;line-height:18px;'
            f'color:#334155;max-width:330px;">{reason_html}</td>'
            "</tr>"
        )

    return "".join(rows)


def build_email_body(
    results: list[TimecardResult],
    report_date: str,
) -> tuple[str, str]:
    approved_count = sum(1 for r in results if r.status == "APPROVED")
    flagged_count = sum(1 for r in results if r.status == "FLAGGED")
    rejected_count = sum(1 for r in results if r.status == "REJECTED")
    total_count = len(results)

    approved_pct = (approved_count / total_count * 100) if total_count else 0
    flagged_pct = (flagged_count / total_count * 100) if total_count else 0
    rejected_pct = (rejected_count / total_count * 100) if total_count else 0

    logger.info(
        "Building time log email: date=%s total=%d approved=%d flagged=%d rejected=%d",
        report_date,
        total_count,
        approved_count,
        flagged_count,
        rejected_count,
    )

    if rejected_count == 0 and flagged_count == 0:
        subject = f"Time Log Verification Report — {report_date} | All Clear"
    else:
        subject = (
            f"Time Log Verification Report — {report_date} | "
            f"{rejected_count} Rejected, {flagged_count} Flagged"
        )

    now_str = datetime.now(_MT).strftime("%Y-%m-%d %H:%M %Z")

    display_date = _format_us_date(report_date)

    summary_cards = (
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
        'style="width:100%;border-collapse:collapse;margin:0 0 24px 0;">'
        "<tr>"
        + _metric_card("Approved", approved_count, approved_pct, "#10b981")
        + _metric_card("Flagged", flagged_count, flagged_pct, "#f59e0b")
        + _metric_card("Rejected", rejected_count, rejected_pct, "#ef4444", "0")
        + "</tr>"
        "</table>"
    )

    if rejected_count == 0 and flagged_count == 0:
        action_title = "All clear"
        action_message = "All timecards passed verification. No action is required."
        action_bg = "#ecfdf5"
        action_border = "#a7f3d0"
        action_badge_bg = "#d1fae5"
        action_badge_text = "#047857"
    else:
        action_title = "Action required"
        action_message = (
            f"{rejected_count} rejected timecard(s) need correction before processing. "
            f"{flagged_count} flagged timecard(s) need review."
        )
        action_bg = "#fffbeb"
        action_border = "#fde68a"
        action_badge_bg = "#fef3c7"
        action_badge_text = "#92400e"

    action_box = (
        f'<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
        f'style="width:100%;border-collapse:separate;border-spacing:0;margin:0 0 28px 0;'
        f'background:{action_bg};border:1px solid {action_border};border-radius:18px;overflow:hidden;">'
        "<tr>"
        '<td style="padding:18px 20px;font-family:Arial,Helvetica,sans-serif;">'
        f'<span style="display:inline-block;background:{action_badge_bg};color:{action_badge_text};'
        f'font-size:11px;line-height:16px;font-weight:800;text-transform:uppercase;'
        f'letter-spacing:0.08em;border-radius:999px;padding:5px 10px;margin:0 0 10px 0;">'
        f'{html.escape(action_title)}</span>'
        f'<div style="font-size:14px;line-height:22px;color:#374151;margin:0;">'
        f'{html.escape(action_message)}</div>'
        "</td>"
        "</tr>"
        "</table>"
    )

    table_section = (
        '<div style="margin:0 0 30px 0;">'
        '<div style="font-family:Arial,Helvetica,sans-serif;font-size:19px;line-height:26px;'
        'font-weight:800;color:#111827;letter-spacing:-0.02em;margin:0 0 12px 0;">'
        "Timecard Details</div>"
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
        'style="width:100%;border-collapse:separate;border-spacing:0;border:1px solid #e5e7eb;'
        'background:#ffffff;border-radius:18px;overflow:hidden;">'
        "<thead>"
        '<tr style="background:#f1f5f9;">'
        '<th style="padding:13px 14px;border-bottom:1px solid #e5e7eb;text-align:left;'
        'font-family:Arial,Helvetica,sans-serif;font-size:11px;line-height:16px;color:#334155;'
        'font-weight:800;">Date</th>'
        '<th style="padding:13px 14px;border-bottom:1px solid #e5e7eb;text-align:left;'
        'font-family:Arial,Helvetica,sans-serif;font-size:11px;line-height:16px;color:#334155;'
        'font-weight:800;">Job Code</th>'
        '<th style="padding:13px 14px;border-bottom:1px solid #e5e7eb;text-align:left;'
        'font-family:Arial,Helvetica,sans-serif;font-size:11px;line-height:16px;color:#334155;'
        'font-weight:800;">Job Description</th>'
        '<th style="padding:13px 14px;border-bottom:1px solid #e5e7eb;text-align:left;'
        'font-family:Arial,Helvetica,sans-serif;font-size:11px;line-height:16px;color:#334155;'
        'font-weight:800;">Foreman</th>'
        '<th style="padding:13px 14px;border-bottom:1px solid #e5e7eb;text-align:center;'
        'font-family:Arial,Helvetica,sans-serif;font-size:11px;line-height:16px;color:#334155;'
        'font-weight:800;">Status</th>'
        '<th style="padding:13px 14px;border-bottom:1px solid #e5e7eb;text-align:left;'
        'font-family:Arial,Helvetica,sans-serif;font-size:11px;line-height:16px;color:#334155;'
        'font-weight:800;">Reasons</th>'
        "</tr>"
        "</thead>"
        "<tbody>"
        + _build_rows(results)
        + "</tbody></table></div>"
    )

    footer = (
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
        'style="width:100%;border-collapse:collapse;border-top:1px solid #e5e7eb;margin-top:8px;">'
        "<tr>"
        '<td style="padding:20px 0 0 0;font-family:Arial,Helvetica,sans-serif;'
        'font-size:12px;line-height:19px;color:#6b7280;">'
        '<strong style="color:#374151;">Construction Operations API</strong><br>'
        f'Automated time log verification report<br>Generated {html.escape(now_str)}'
        "</td>"
        "</tr>"
        "</table>"
    )

    html_body = (
        '<div style="margin:0;padding:0;background:#eef2f7;">'
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
        'style="width:100%;border-collapse:collapse;background:#eef2f7;">'
        "<tr>"
        '<td align="center" style="padding:32px 14px;">'
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
        'style="width:100%;max-width:980px;border-collapse:separate;border-spacing:0;'
        'background:#ffffff;border-radius:24px;overflow:hidden;">'
        "<tr>"
        '<td style="padding:0;font-family:Arial,Helvetica,sans-serif;">'
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
        'style="width:100%;border-collapse:collapse;background:#0f172a;'
        'background-image:linear-gradient(135deg,#0f172a 0%,#155e75 48%,#10b981 100%);">'
        "<tr>"
        '<td style="padding:34px 34px 34px 34px;font-family:Arial,Helvetica,sans-serif;">'
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
        'style="width:100%;border-collapse:collapse;">'
        "<tr>"
        '<td style="vertical-align:top;">'
        '<div style="font-size:12px;line-height:17px;color:#d1fae5;font-weight:800;'
        'text-transform:uppercase;letter-spacing:0.12em;margin:0 0 34px 0;">'
        "Construction Operations API</div>"
        '<div style="font-size:42px;line-height:48px;color:#ffffff;font-weight:800;'
        'letter-spacing:-0.05em;margin:0 0 14px 0;">Time Log Verification</div>'
        f'<div style="font-size:20px;line-height:28px;color:#ecfeff;font-weight:700;margin:0;">'
        f'{html.escape(display_date)}</div>'
        "</td>"
        '<td style="vertical-align:top;text-align:right;white-space:nowrap;">'
        '<span style="display:inline-block;background:rgba(255,255,255,0.18);color:#ffffff;'
        'font-size:11px;line-height:16px;font-weight:800;text-transform:uppercase;'
        'letter-spacing:0.08em;border-radius:999px;padding:8px 12px;">Email Report</span>'
        "</td>"
        "</tr>"
        "</table>"
        "</td>"
        "</tr>"
        "</table>"
        "</td>"
        "</tr>"
        "<tr>"
        '<td style="padding:30px 34px 34px 34px;font-family:Arial,Helvetica,sans-serif;background:#ffffff;">'
        f'<div style="font-size:14px;line-height:21px;color:#6b7280;margin:0 0 20px 0;">'
        f'Report date: <strong style="color:#111827;">{html.escape(_format_us_date(report_date))}</strong>'
        f' &nbsp;|&nbsp; Total timecards: <strong style="color:#111827;">{total_count}</strong>'
        "</div>"
        + summary_cards
        + action_box
        + table_section
        + footer
        + "</td>"
        "</tr>"
        "</table>"
        "</td>"
        "</tr>"
        "</table>"
        "</div>"
    )

    logger.debug("Email body built successfully for date=%s", report_date)
    return subject, html_body