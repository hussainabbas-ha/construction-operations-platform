"""
notifications/projection_email_template.py

Compact column-based email template for projection reports.

Keeps the same projection columns:
Job Code, Job Name, Cost Code, Description, Budgeted Qty,
Qty To Date, Completion, Expected Cost, Actual Cost, Variance.

Only changes email formatting/size.
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


def _fmt_qty(value) -> str:
    try:
        return f"{float(value or 0):,.2f}"
    except (TypeError, ValueError):
        return "0.00"


def _fmt_pct(value) -> str:
    try:
        return f"{float(value or 0):.2f}%"
    except (TypeError, ValueError):
        return "0.00%"


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


def _build_projection_rows(items: list) -> str:
    rows: list[str] = []

    for item in items:
        job_code = str(_get(item, "job_code", "") or "")
        job_name = str(_get(item, "job_name", "") or "")
        cost_code = str(_get(item, "cost_code", "") or "")
        description = str(_get(item, "cost_code_description", "") or "")

        budgeted_qty = _get(item, "budgeted_quantity", 0)
        qty_to_date = _get(item, "quantity", 0)
        completion_pct = float(_get(item, "completion_pct", 0) or 0)

        expected = _get(item, "expected", 0)
        actual = _get(item, "actual", 0)
        variance = float(_get(item, "variance", 0) or 0)

        variance_color = "#059669" if variance >= 0 else "#dc2626"
        variance_text = (
            f"+{variance:,.2f}"
            if variance >= 0
            else f"-{abs(variance):,.2f}"
        )

        if completion_pct >= 100:
            pct_bg = "#fee2e2"
            pct_color = "#dc2626"
        elif completion_pct >= 75:
            pct_bg = "#ffedd5"
            pct_color = "#c2410c"
        else:
            pct_bg = "#eef2f7"
            pct_color = "#475569"

        pct_html = (
            f'<span style="display:inline-block;background:{pct_bg};color:{pct_color};'
            f'border-radius:999px;padding:3px 7px;font-size:9px;line-height:12px;'
            f'font-weight:800;white-space:nowrap;">{html.escape(_fmt_pct(completion_pct))}</span>'
        )

        rows.append(
            "<tr>"
            f'<td style="padding:9px 8px;border-bottom:1px solid #eef2f7;'
            f'font-family:Arial,Helvetica,sans-serif;font-size:10px;line-height:15px;'
            f'color:#334155;font-weight:800;white-space:nowrap;">{html.escape(job_code)}</td>'

            f'<td style="padding:9px 8px;border-bottom:1px solid #eef2f7;'
            f'font-family:Arial,Helvetica,sans-serif;font-size:10px;line-height:15px;'
            f'color:#334155;max-width:115px;">{html.escape(job_name)}</td>'

            f'<td style="padding:9px 8px;border-bottom:1px solid #eef2f7;'
            f'font-family:Arial,Helvetica,sans-serif;font-size:10px;line-height:15px;'
            f'color:#334155;font-weight:800;white-space:nowrap;">{html.escape(cost_code)}</td>'

            f'<td style="padding:9px 8px;border-bottom:1px solid #eef2f7;'
            f'font-family:Arial,Helvetica,sans-serif;font-size:10px;line-height:15px;'
            f'color:#475569;max-width:150px;">{html.escape(description)}</td>'

            f'<td style="padding:9px 8px;border-bottom:1px solid #eef2f7;'
            f'font-family:Arial,Helvetica,sans-serif;font-size:10px;line-height:15px;'
            f'color:#334155;text-align:right;white-space:nowrap;">{html.escape(_fmt_qty(budgeted_qty))}</td>'

            f'<td style="padding:9px 8px;border-bottom:1px solid #eef2f7;'
            f'font-family:Arial,Helvetica,sans-serif;font-size:10px;line-height:15px;'
            f'color:#334155;text-align:right;white-space:nowrap;">{html.escape(_fmt_qty(qty_to_date))}</td>'

            f'<td style="padding:9px 8px;border-bottom:1px solid #eef2f7;'
            f'font-family:Arial,Helvetica,sans-serif;font-size:10px;line-height:15px;'
            f'text-align:center;white-space:nowrap;">{pct_html}</td>'

            f'<td style="padding:9px 8px;border-bottom:1px solid #eef2f7;'
            f'font-family:Arial,Helvetica,sans-serif;font-size:10px;line-height:15px;'
            f'color:#334155;text-align:right;white-space:nowrap;">{_fmt_money(expected)}</td>'

            f'<td style="padding:9px 8px;border-bottom:1px solid #eef2f7;'
            f'font-family:Arial,Helvetica,sans-serif;font-size:10px;line-height:15px;'
            f'color:#334155;text-align:right;white-space:nowrap;">{_fmt_money(actual)}</td>'

            f'<td style="padding:9px 8px;border-bottom:1px solid #eef2f7;'
            f'font-family:Arial,Helvetica,sans-serif;font-size:10px;line-height:15px;'
            f'color:{variance_color};font-weight:800;text-align:right;white-space:nowrap;">'
            f'{html.escape(variance_text)}</td>'
            "</tr>"
        )

    return "".join(rows)


def build_projection_email_body(
    *,
    tracking_month: str,
    items: list,
    comments: str = "",
    triggered_by: str = "Manual",
) -> tuple[str, str]:
    """Build subject + HTML body for a projection report email."""
    on_track_count = sum(1 for item in items if _get(item, "status", "") == "ON_TRACK")
    at_risk_count = sum(1 for item in items if _get(item, "status", "") == "AT_RISK")
    over_budget_count = sum(1 for item in items if _get(item, "status", "") == "OVER_BUDGET")
    total_count = len(items)

    logger.info(
        "Building projection email: period=%s total=%d on_track=%d at_risk=%d over_budget=%d",
        tracking_month,
        total_count,
        on_track_count,
        at_risk_count,
        over_budget_count,
    )

    if over_budget_count > 0:
        subject = f"Projection Alert — {over_budget_count} Over Budget | {tracking_month}"
    elif at_risk_count > 0:
        subject = f"Projection Report — {at_risk_count} At Risk | {tracking_month}"
    else:
        subject = f"Projection Report — All Clear | {tracking_month}"

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
            "At Risk",
            at_risk_count,
            "#ea580c",
            "linear-gradient(135deg,#fff7ed 0%,#ffedd5 45%,#ffffff 100%)",
            "#fed7aa",
        )
        + _metric_card(
            "Over Budget",
            over_budget_count,
            "#dc2626",
            "linear-gradient(135deg,#fff1f2 0%,#ffe4e6 45%,#ffffff 100%)",
            "#fecdd3",
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
        "Projection Review</span>"
        f'<div style="font-size:12px;line-height:18px;color:#374151;margin:0;">'
        f"{at_risk_count} row(s) are at risk and {over_budget_count} row(s) are over budget."
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
        "Projection Details</div>"
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
        'style="width:100%;border-collapse:separate;border-spacing:0;border:1px solid #e5e7eb;'
        'background:#ffffff;border-radius:14px;overflow:hidden;table-layout:auto;">'
        "<thead>"
        '<tr style="background:linear-gradient(135deg,#f9fafb 0%,#f3f4f6 100%);">'
        '<th style="padding:9px 8px;border-bottom:1px solid #e5e7eb;text-align:left;'
        'font-family:Arial,Helvetica,sans-serif;font-size:9px;line-height:13px;'
        'color:#6b7280;text-transform:uppercase;letter-spacing:0.06em;">Job<br>Code</th>'
        '<th style="padding:9px 8px;border-bottom:1px solid #e5e7eb;text-align:left;'
        'font-family:Arial,Helvetica,sans-serif;font-size:9px;line-height:13px;'
        'color:#6b7280;text-transform:uppercase;letter-spacing:0.06em;">Job Name</th>'
        '<th style="padding:9px 8px;border-bottom:1px solid #e5e7eb;text-align:left;'
        'font-family:Arial,Helvetica,sans-serif;font-size:9px;line-height:13px;'
        'color:#6b7280;text-transform:uppercase;letter-spacing:0.06em;">Cost Code</th>'
        '<th style="padding:9px 8px;border-bottom:1px solid #e5e7eb;text-align:left;'
        'font-family:Arial,Helvetica,sans-serif;font-size:9px;line-height:13px;'
        'color:#6b7280;text-transform:uppercase;letter-spacing:0.06em;">Description</th>'
        '<th style="padding:9px 8px;border-bottom:1px solid #e5e7eb;text-align:right;'
        'font-family:Arial,Helvetica,sans-serif;font-size:9px;line-height:13px;'
        'color:#6b7280;text-transform:uppercase;letter-spacing:0.06em;">Budgeted<br>Qty</th>'
        '<th style="padding:9px 8px;border-bottom:1px solid #e5e7eb;text-align:right;'
        'font-family:Arial,Helvetica,sans-serif;font-size:9px;line-height:13px;'
        'color:#6b7280;text-transform:uppercase;letter-spacing:0.06em;">Qty<br>To Date</th>'
        '<th style="padding:9px 8px;border-bottom:1px solid #e5e7eb;text-align:center;'
        'font-family:Arial,Helvetica,sans-serif;font-size:9px;line-height:13px;'
        'color:#6b7280;text-transform:uppercase;letter-spacing:0.06em;">Completion</th>'
        '<th style="padding:9px 8px;border-bottom:1px solid #e5e7eb;text-align:right;'
        'font-family:Arial,Helvetica,sans-serif;font-size:9px;line-height:13px;'
        'color:#6b7280;text-transform:uppercase;letter-spacing:0.06em;">Expected<br>Cost</th>'
        '<th style="padding:9px 8px;border-bottom:1px solid #e5e7eb;text-align:right;'
        'font-family:Arial,Helvetica,sans-serif;font-size:9px;line-height:13px;'
        'color:#6b7280;text-transform:uppercase;letter-spacing:0.06em;">Actual<br>Cost</th>'
        '<th style="padding:9px 8px;border-bottom:1px solid #e5e7eb;text-align:right;'
        'font-family:Arial,Helvetica,sans-serif;font-size:9px;line-height:13px;'
        'color:#6b7280;text-transform:uppercase;letter-spacing:0.06em;">Variance</th>'
        "</tr>"
        "</thead>"
        "<tbody>"
        + _build_projection_rows(items)
        + "</tbody></table></div>"
    )

    footer = (
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
        'style="width:100%;border-collapse:collapse;border-top:1px solid #e5e7eb;margin-top:6px;">'
        "<tr>"
        '<td style="padding:15px 0 0 0;font-family:Arial,Helvetica,sans-serif;'
        'font-size:11px;line-height:17px;color:#6b7280;">'
        '<strong style="color:#374151;">Construction Operations API</strong><br>'
        f"Automated projection report<br>Generated {html.escape(now_str)} "
        f"[{html.escape(triggered_by)}]"
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
        'style="width:100%;max-width:900px;border-collapse:separate;border-spacing:0;'
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
        'letter-spacing:-0.05em;margin:0 0 9px 0;">Projections</div>'
        f'<div style="font-size:16px;line-height:23px;color:#ecfeff;font-weight:700;margin:0;">'
        f"{html.escape(tracking_month)}</div>"
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
        f'Reporting period: <strong style="color:#111827;">{html.escape(tracking_month)}</strong>'
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

    logger.debug("Projection email body built successfully")
    return subject, html_body