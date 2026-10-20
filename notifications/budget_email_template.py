"""
notifications/budget_email_template.py

Compact email template for budget tracking reports.

Important:
- Uses item.expected_budget for the Cost / Budget column.
- Does NOT use item.budgeted_all_cost in the email table.
- Keeps the same budget module/dashboard values.
"""

from __future__ import annotations

import html
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

_MT = ZoneInfo("America/Denver")


def _get(item, key: str, default=None):
    if isinstance(item, dict):
        return item.get(key, default)
    return getattr(item, key, default)


def _fmt_money(value) -> str:
    try:
        return f"${float(value or 0):,.2f}"
    except (TypeError, ValueError):
        return "$0.00"


def _fmt_number(value, digits: int = 2) -> str:
    try:
        return f"{float(value or 0):,.{digits}f}"
    except (TypeError, ValueError):
        return f"{0:.{digits}f}"


def _fmt_pct(value, digits: int = 0) -> str:
    if value is None:
        return "0%" if digits == 0 else "0.00%"
    try:
        return f"{float(value):.{digits}f}%"
    except (TypeError, ValueError):
        return "0%" if digits == 0 else "0.00%"


def _period_label(period_start: str, period_end: str) -> str:
    if period_start == period_end:
        return period_start
    return f"{period_start} to {period_end}"


def _metric_card(
    label: str,
    value: int,
    text_color: str,
    gradient: str,
    border_color: str,
    padding_right: str = "8px",
) -> str:
    return (
        f'<td style="width:33.33%;padding:0 {padding_right} 0 0;vertical-align:top;">'
        f'<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
        f'style="width:100%;border-collapse:separate;border-spacing:0;background:{gradient};'
        f'border:1px solid {border_color};border-radius:14px;overflow:hidden;">'
        "<tr>"
        '<td style="padding:13px 14px;font-family:Arial,Helvetica,sans-serif;">'
        f'<div style="font-size:10px;line-height:14px;color:#64748b;font-weight:800;'
        f'text-transform:uppercase;letter-spacing:0.08em;">{html.escape(label)}</div>'
        f'<div style="margin-top:6px;font-size:28px;line-height:32px;color:{text_color};'
        f'font-weight:800;letter-spacing:-0.04em;">{value}</div>'
        "</td>"
        "</tr>"
        "</table>"
        "</td>"
    )


def _status_badge(status: str) -> str:
    status = (status or "").upper()

    if status == "ON_TRACK":
        bg, color, label = "#dcfce7", "#047857", "ON TRACK"
    else:
        bg, color, label = "#fee2e2", "#dc2626", "OVER RISK"

    return (
        f'<span style="display:inline-block;background:{bg};color:{color};'
        f'font-size:9px;line-height:13px;font-weight:800;text-transform:uppercase;'
        f'letter-spacing:0.04em;border-radius:999px;padding:4px 7px;white-space:nowrap;">'
        f"{html.escape(label)}</span>"
    )


def _build_budget_rows(items: list) -> str:
    rows: list[str] = []

    for item in items:
        job = str(_get(item, "job_name", "") or "")
        cost_code = str(_get(item, "cost_code", "") or "")
        description = str(_get(item, "cost_code_description", "") or "")

        actual_cost = float(_get(item, "actual_cost", 0) or 0)

        # IMPORTANT:
        # This must use expected_budget because this is the dashboard/module value.
        # Do not use budgeted_all_cost here.
        expected_budget = float(_get(item, "expected_budget", 0) or 0)

        utilization = _get(item, "utilization_percentage", None)
        variance = float(_get(item, "variance", 0) or 0)
        loss_amount = float(_get(item, "loss_amount", 0) or 0)
        loss_pct = _get(item, "loss_pct", None)
        status = str(_get(item, "status", "ON_TRACK") or "ON_TRACK")

        variance_color = "#059669" if variance >= 0 else "#dc2626"
        variance_text = (
            f"+{_fmt_number(variance, 2)}"
            if variance >= 0
            else f"-{_fmt_number(abs(variance), 2)}"
        )

        if expected_budget <= 0 and actual_cost > 0:
            utilization_badge = (
                '<div style="display:inline-block;margin-top:4px;background:#fee2e2;'
                'color:#dc2626;border-radius:999px;padding:3px 8px;font-size:10px;'
                'line-height:13px;font-weight:800;">Unbudgeted Cost</div>'
            )
        else:
            utilization_badge = (
                f'<div style="display:inline-block;margin-top:4px;background:#eef2f7;'
                f'color:#475569;border-radius:999px;padding:3px 8px;font-size:10px;'
                f'line-height:13px;font-weight:800;">'
                f'{html.escape(_fmt_pct(utilization, 0))} Utilized</div>'
            )

        cost_budget_html = (
            f'<div style="font-weight:800;color:#334155;white-space:nowrap;">'
            f'{_fmt_money(actual_cost)} / {_fmt_money(expected_budget)}</div>'
            f'{utilization_badge}'
        )

        if loss_amount > 0:
            loss_html = (
                f'<div style="color:#dc2626;font-weight:800;white-space:nowrap;">'
                f'{_fmt_money(loss_amount)}</div>'
                f'<div style="display:inline-block;margin-top:4px;background:#fee2e2;'
                f'color:#dc2626;border-radius:999px;padding:3px 8px;font-size:10px;'
                f'line-height:13px;font-weight:700;">{html.escape(_fmt_pct(loss_pct, 2))}</div>'
            )
        else:
            loss_html = '<span style="color:#94a3b8;font-weight:700;">-</span>'

        rows.append(
            "<tr>"
            f'<td style="padding:9px 8px;border-bottom:1px solid #eef2f7;'
            f'font-family:Arial,Helvetica,sans-serif;font-size:10px;line-height:15px;'
            f'color:#334155;max-width:185px;">{html.escape(job)}</td>'

            f'<td style="padding:9px 8px;border-bottom:1px solid #eef2f7;'
            f'font-family:Arial,Helvetica,sans-serif;font-size:10px;line-height:15px;'
            f'color:#334155;font-weight:800;white-space:nowrap;">{html.escape(cost_code)}</td>'

            f'<td style="padding:9px 8px;border-bottom:1px solid #eef2f7;'
            f'font-family:Arial,Helvetica,sans-serif;font-size:10px;line-height:15px;'
            f'color:#475569;max-width:170px;">{html.escape(description)}</td>'

            f'<td style="padding:9px 8px;border-bottom:1px solid #eef2f7;'
            f'font-family:Arial,Helvetica,sans-serif;font-size:10px;line-height:15px;'
            f'color:#334155;white-space:nowrap;">{cost_budget_html}</td>'

            f'<td style="padding:9px 8px;border-bottom:1px solid #eef2f7;'
            f'font-family:Arial,Helvetica,sans-serif;font-size:10px;line-height:15px;'
            f'color:{variance_color};font-weight:800;text-align:right;white-space:nowrap;">'
            f'{html.escape(variance_text)}</td>'

            f'<td style="padding:9px 8px;border-bottom:1px solid #eef2f7;'
            f'font-family:Arial,Helvetica,sans-serif;font-size:10px;line-height:15px;'
            f'text-align:right;white-space:nowrap;">{loss_html}</td>'

            f'<td style="padding:9px 8px;border-bottom:1px solid #eef2f7;'
            f'font-family:Arial,Helvetica,sans-serif;font-size:10px;line-height:15px;'
            f'text-align:center;white-space:nowrap;">{_status_badge(status)}</td>'
            "</tr>"
        )

    return "".join(rows)


def build_budget_email_body(
    *,
    period_start: str,
    period_end: str,
    items: list,
    comments: str = "",
) -> tuple[str, str]:
    """Build subject + HTML body for budget report email."""
    on_track_count = sum(1 for item in items if _get(item, "status", "") == "ON_TRACK")
    over_risk_count = sum(1 for item in items if _get(item, "status", "") == "OVER_RISK")
    total_count = len(items)

    logger.info(
        "Building budget email: period=%s to %s total=%d on_track=%d over_risk=%d",
        period_start,
        period_end,
        total_count,
        on_track_count,
        over_risk_count,
    )

    period_display = _period_label(period_start, period_end)
    subject = f"Budget Tracking Report — {period_display} | {over_risk_count} Over Risk"
    now_str = datetime.now(_MT).strftime("%Y-%m-%d %H:%M %Z")

    summary_cards = (
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
        'style="width:100%;border-collapse:collapse;margin:0 0 18px 0;">'
        "<tr>"
        + _metric_card(
            "On Track",
            on_track_count,
            "#059669",
            "linear-gradient(135deg,#ecfdf5 0%,#d1fae5 45%,#ffffff 100%)",
            "#a7f3d0",
        )
        + _metric_card(
            "Over Risk",
            over_risk_count,
            "#dc2626",
            "linear-gradient(135deg,#fff1f2 0%,#ffe4e6 45%,#ffffff 100%)",
            "#fecdd3",
        )
        + _metric_card(
            "Total",
            total_count,
            "#0f172a",
            "linear-gradient(135deg,#f8fafc 0%,#e2e8f0 45%,#ffffff 100%)",
            "#cbd5e1",
            "0",
        )
        + "</tr>"
        "</table>"
    )

    action_box = (
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
        'style="width:100%;border-collapse:separate;border-spacing:0;margin:0 0 18px 0;'
        'background:#fffbeb;border:1px solid #fde68a;border-radius:14px;overflow:hidden;">'
        "<tr>"
        '<td style="padding:13px 14px;font-family:Arial,Helvetica,sans-serif;">'
        '<span style="display:inline-block;background:#fef3c7;color:#92400e;'
        'font-size:10px;line-height:14px;font-weight:800;text-transform:uppercase;'
        'letter-spacing:0.08em;border-radius:999px;padding:5px 9px;margin:0 0 7px 0;">'
        "Budget Review</span>"
        f'<div style="font-size:12px;line-height:18px;color:#374151;margin:0;">'
        f"{over_risk_count} cost code(s) are marked over risk and should be reviewed."
        "</div>"
        "</td>"
        "</tr>"
        "</table>"
    )

    comments_section = ""
    if comments and comments.strip():
        comments_section = (
            '<div style="margin:0 0 18px 0;padding:12px 14px;background:#f8fafc;'
            'border:1px solid #e2e8f0;border-radius:12px;">'
            '<div style="font-family:Arial,Helvetica,sans-serif;font-size:10px;'
            'line-height:14px;color:#64748b;font-weight:800;text-transform:uppercase;'
            'letter-spacing:0.08em;margin:0 0 5px 0;">Comments</div>'
            f'<div style="font-family:Arial,Helvetica,sans-serif;font-size:12px;'
            f'line-height:18px;color:#475569;margin:0;">{html.escape(comments)}</div>'
            "</div>"
        )

    table = (
        '<div style="margin:0 0 20px 0;">'
        '<div style="font-family:Arial,Helvetica,sans-serif;font-size:16px;line-height:22px;'
        'font-weight:800;color:#111827;letter-spacing:-0.02em;margin:0 0 10px 0;">'
        "Budget Details</div>"
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
        'style="width:100%;border-collapse:separate;border-spacing:0;border:1px solid #e5e7eb;'
        'background:#ffffff;border-radius:14px;overflow:hidden;table-layout:auto;">'
        "<thead>"
        '<tr style="background:linear-gradient(135deg,#f9fafb 0%,#f3f4f6 100%);">'
        '<th style="padding:9px 8px;border-bottom:1px solid #e5e7eb;text-align:left;'
        'font-family:Arial,Helvetica,sans-serif;font-size:9px;line-height:13px;'
        'color:#6b7280;text-transform:uppercase;letter-spacing:0.06em;">Job</th>'
        '<th style="padding:9px 8px;border-bottom:1px solid #e5e7eb;text-align:left;'
        'font-family:Arial,Helvetica,sans-serif;font-size:9px;line-height:13px;'
        'color:#6b7280;text-transform:uppercase;letter-spacing:0.06em;">Cost Code</th>'
        '<th style="padding:9px 8px;border-bottom:1px solid #e5e7eb;text-align:left;'
        'font-family:Arial,Helvetica,sans-serif;font-size:9px;line-height:13px;'
        'color:#6b7280;text-transform:uppercase;letter-spacing:0.06em;">Description</th>'
        '<th style="padding:9px 8px;border-bottom:1px solid #e5e7eb;text-align:left;'
        'font-family:Arial,Helvetica,sans-serif;font-size:9px;line-height:13px;'
        'color:#6b7280;text-transform:uppercase;letter-spacing:0.06em;">Cost / Budget</th>'
        '<th style="padding:9px 8px;border-bottom:1px solid #e5e7eb;text-align:right;'
        'font-family:Arial,Helvetica,sans-serif;font-size:9px;line-height:13px;'
        'color:#6b7280;text-transform:uppercase;letter-spacing:0.06em;">Variance</th>'
        '<th style="padding:9px 8px;border-bottom:1px solid #e5e7eb;text-align:right;'
        'font-family:Arial,Helvetica,sans-serif;font-size:9px;line-height:13px;'
        'color:#6b7280;text-transform:uppercase;letter-spacing:0.06em;">Loss</th>'
        '<th style="padding:9px 8px;border-bottom:1px solid #e5e7eb;text-align:center;'
        'font-family:Arial,Helvetica,sans-serif;font-size:9px;line-height:13px;'
        'color:#6b7280;text-transform:uppercase;letter-spacing:0.06em;">Status</th>'
        "</tr>"
        "</thead>"
        "<tbody>"
        + _build_budget_rows(items)
        + "</tbody></table></div>"
    )

    footer = (
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
        'style="width:100%;border-collapse:collapse;border-top:1px solid #e5e7eb;margin-top:6px;">'
        "<tr>"
        '<td style="padding:15px 0 0 0;font-family:Arial,Helvetica,sans-serif;'
        'font-size:11px;line-height:17px;color:#6b7280;">'
        '<strong style="color:#374151;">Construction Operations API</strong><br>'
        f"Automated budget tracking report<br>Generated {html.escape(now_str)}"
        "</td>"
        "</tr>"
        "</table>"
    )

    html_body = (
        '<div style="margin:0;padding:0;background:#eef2f7;">'
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
        'style="width:100%;border-collapse:collapse;background:#eef2f7;">'
        "<tr>"
        '<td align="center" style="padding:22px 8px;">'
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
        'style="width:100%;max-width:860px;border-collapse:separate;border-spacing:0;'
        'background:#ffffff;border-radius:20px;overflow:hidden;">'

        # Header
        "<tr>"
        '<td style="padding:0;font-family:Arial,Helvetica,sans-serif;">'
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
        'style="width:100%;border-collapse:collapse;background:#0f172a;'
        'background-image:linear-gradient(135deg,#0f172a 0%,#155e75 48%,#10b981 100%);">'
        "<tr>"
        '<td style="padding:27px 30px;font-family:Arial,Helvetica,sans-serif;">'
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
        'style="width:100%;border-collapse:collapse;">'
        "<tr>"
        '<td style="vertical-align:top;">'
        '<div style="font-size:11px;line-height:16px;color:#d1fae5;font-weight:800;'
        'text-transform:uppercase;letter-spacing:0.12em;margin:0 0 24px 0;">'
        "Construction Operations API</div>"
        '<div style="font-size:33px;line-height:38px;color:#ffffff;font-weight:800;'
        'letter-spacing:-0.05em;margin:0 0 9px 0;">Budget Tracking</div>'
        f'<div style="font-size:16px;line-height:23px;color:#ecfeff;font-weight:700;margin:0;">'
        f"{html.escape(period_display)}</div>"
        "</td>"
        '<td style="vertical-align:top;text-align:right;white-space:nowrap;">'
        '<span style="display:inline-block;background:rgba(255,255,255,0.18);color:#ffffff;'
        'font-size:10px;line-height:14px;font-weight:800;text-transform:uppercase;'
        'letter-spacing:0.08em;border-radius:999px;padding:7px 11px;">Email Report</span>'
        "</td>"
        "</tr>"
        "</table>"
        "</td>"
        "</tr>"
        "</table>"
        "</td>"
        "</tr>"

        # Body
        "<tr>"
        '<td style="padding:22px 24px 26px 24px;font-family:Arial,Helvetica,sans-serif;background:#ffffff;">'
        f'<div style="font-size:12px;line-height:19px;color:#6b7280;margin:0 0 15px 0;">'
        f'Date range: <strong style="color:#111827;">{html.escape(period_display)}</strong>'
        f' &nbsp;|&nbsp; Total rows: <strong style="color:#111827;">{total_count}</strong>'
        "</div>"
        + summary_cards
        + action_box
        + comments_section
        + table
        + footer
        + "</td>"
        "</tr>"
        "</table>"
        "</td>"
        "</tr>"
        "</table>"
        "</div>"
    )

    logger.debug("Budget email body built successfully")
    return subject, html_body