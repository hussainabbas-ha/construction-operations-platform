"""
notifications/quantity_email_template.py

Compact column-based email template for weekly quantity tracking alerts.

Changes:
- Keeps the same column/table format.
- Replaces Remaining Qty with Variance.
- Uses tighter spacing, smaller font sizes, and compact widths so the table is easier to view in email.
"""
from __future__ import annotations

import html
from datetime import datetime
from zoneinfo import ZoneInfo

_MT = ZoneInfo("America/Denver")


def _get(item, key: str, default=None):
    if isinstance(item, dict):
        return item.get(key, default)
    return getattr(item, key, default)


def _fmt_qty(value) -> str:
    try:
        return f"{float(value or 0):,.2f}"
    except (TypeError, ValueError):
        return "0.00"


def _fmt_pct(value) -> str:
    try:
        return f"{float(value or 0):.1f}%"
    except (TypeError, ValueError):
        return "0.0%"


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

    if status == "OVER_RISK":
        bg, color, label = "#fee2e2", "#dc2626", "OVER RISK"
    elif status == "NEAR_COMPLETION":
        bg, color, label = "#ffedd5", "#c2410c", "NEAR COMPLETION"
    else:
        bg, color, label = "#dcfce7", "#047857", "ON TRACK"

    return (
        f'<span style="display:inline-block;background:{bg};color:{color};'
        f'font-size:9px;line-height:13px;font-weight:800;text-transform:uppercase;'
        f'letter-spacing:0.04em;border-radius:999px;padding:4px 7px;white-space:nowrap;">'
        f"{html.escape(label)}</span>"
    )


def _build_quantity_rows(alerts: list) -> str:
    rows: list[str] = []

    for item in alerts:
        job_code = str(_get(item, "job_code", "") or "")
        cost_code = str(_get(item, "cost_code", "") or "")
        description = str(_get(item, "description", "") or "")
        cost_type = str(_get(item, "cost_type", "") or "").replace("_", " ").title()
        unit = str(_get(item, "unit", "") or "")

        planned = float(_get(item, "planned_quantity", 0) or 0)
        installed = float(_get(item, "installed_quantity", 0) or 0)
        pct = float(_get(item, "percent_complete", 0) or 0)
        status = str(_get(item, "status", "") or "")

        # Variance convention:
        # positive = quantity still available under plan
        # negative = installed quantity exceeded planned quantity
        variance = planned - installed
        variance_color = "#059669" if variance >= 0 else "#dc2626"
        variance_text = (
            f"+{_fmt_qty(variance)}"
            if variance >= 0
            else f"-{_fmt_qty(abs(variance))}"
        )

        pct_color = "#dc2626" if pct >= 100 else "#c2410c" if pct >= 75 else "#334155"
        pct_bg = "#fee2e2" if pct >= 100 else "#ffedd5" if pct >= 75 else "#eef2f7"
        pct_html = (
            f'<span style="display:inline-block;background:{pct_bg};color:{pct_color};'
            f'border-radius:999px;padding:3px 7px;font-size:9px;line-height:12px;'
            f'font-weight:800;white-space:nowrap;">{html.escape(_fmt_pct(pct))}</span>'
        )

        variance_html = (
            f'<span style="color:{variance_color};font-weight:800;white-space:nowrap;">'
            f'{html.escape(variance_text)} {html.escape(unit)}</span>'
        )

        rows.append(
            "<tr>"
            f'<td style="padding:9px 8px;border-bottom:1px solid #eef2f7;'
            f'font-family:Arial,Helvetica,sans-serif;font-size:10px;line-height:15px;'
            f'color:#334155;font-weight:800;white-space:nowrap;">{html.escape(job_code)}</td>'

            f'<td style="padding:9px 8px;border-bottom:1px solid #eef2f7;'
            f'font-family:Arial,Helvetica,sans-serif;font-size:10px;line-height:15px;'
            f'color:#334155;font-weight:800;white-space:nowrap;">{html.escape(cost_code)}</td>'

            f'<td style="padding:9px 8px;border-bottom:1px solid #eef2f7;'
            f'font-family:Arial,Helvetica,sans-serif;font-size:10px;line-height:15px;'
            f'color:#475569;max-width:190px;">{html.escape(description)}</td>'

            f'<td style="padding:9px 8px;border-bottom:1px solid #eef2f7;'
            f'font-family:Arial,Helvetica,sans-serif;font-size:10px;line-height:15px;'
            f'color:#334155;white-space:nowrap;">{html.escape(cost_type)}</td>'

            f'<td style="padding:9px 8px;border-bottom:1px solid #eef2f7;'
            f'font-family:Arial,Helvetica,sans-serif;font-size:10px;line-height:15px;'
            f'color:#334155;text-align:right;white-space:nowrap;">'
            f'{html.escape(_fmt_qty(planned))} {html.escape(unit)}</td>'

            f'<td style="padding:9px 8px;border-bottom:1px solid #eef2f7;'
            f'font-family:Arial,Helvetica,sans-serif;font-size:10px;line-height:15px;'
            f'color:#334155;text-align:right;white-space:nowrap;">'
            f'{html.escape(_fmt_qty(installed))} {html.escape(unit)}</td>'

            f'<td style="padding:9px 8px;border-bottom:1px solid #eef2f7;'
            f'font-family:Arial,Helvetica,sans-serif;font-size:10px;line-height:15px;'
            f'text-align:right;white-space:nowrap;">{variance_html}</td>'

            f'<td style="padding:9px 8px;border-bottom:1px solid #eef2f7;'
            f'font-family:Arial,Helvetica,sans-serif;font-size:10px;line-height:15px;'
            f'text-align:center;white-space:nowrap;">{pct_html}</td>'

            f'<td style="padding:9px 8px;border-bottom:1px solid #eef2f7;'
            f'font-family:Arial,Helvetica,sans-serif;font-size:10px;line-height:15px;'
            f'text-align:center;white-space:nowrap;">{_status_badge(status)}</td>'
            "</tr>"
        )

    return "".join(rows)


def build_quantity_alert_email(
    alerts: list,
    period_start: str,
    period_end: str,
) -> tuple[str, str]:
    """Build subject + HTML body for the weekly quantity alert email."""
    now_str = datetime.now(_MT).strftime("%Y-%m-%d %H:%M %Z")

    total_alerts = len(alerts)
    over_risk = sum(1 for item in alerts if _get(item, "status", "") == "OVER_RISK")
    near_completion = sum(1 for item in alerts if _get(item, "status", "") == "NEAR_COMPLETION")

    subject = f"Quantity Alert — {total_alerts} Item(s) at or above 75% | Week ending {period_end}"

    summary_cards = (
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
        'style="width:100%;border-collapse:collapse;margin:0 0 18px 0;">'
        "<tr>"
        + _metric_card(
            "Near Completion",
            near_completion,
            "#ea580c",
            "linear-gradient(135deg,#fff7ed 0%,#ffedd5 45%,#ffffff 100%)",
            "#fed7aa",
        )
        + _metric_card(
            "Over Risk",
            over_risk,
            "#dc2626",
            "linear-gradient(135deg,#fff1f2 0%,#ffe4e6 45%,#ffffff 100%)",
            "#fecdd3",
        )
        + _metric_card(
            "Total Alerts",
            total_alerts,
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
        "Quantity Review Required</span>"
        f'<div style="font-size:12px;line-height:18px;color:#374151;margin:0;">'
        f"{total_alerts} cost code(s) have reached 75% or more of planned quantity. "
        "Variance shows planned quantity minus installed quantity.</div>"
        "</td>"
        "</tr>"
        "</table>"
    )

    table = (
        '<div style="margin:0 0 20px 0;">'
        '<div style="font-family:Arial,Helvetica,sans-serif;font-size:16px;line-height:22px;'
        'font-weight:800;color:#111827;letter-spacing:-0.02em;margin:0 0 10px 0;">'
        "Quantity Alert Details</div>"
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
        'color:#6b7280;text-transform:uppercase;letter-spacing:0.06em;">Type</th>'
        '<th style="padding:9px 8px;border-bottom:1px solid #e5e7eb;text-align:right;'
        'font-family:Arial,Helvetica,sans-serif;font-size:9px;line-height:13px;'
        'color:#6b7280;text-transform:uppercase;letter-spacing:0.06em;">Planned Qty</th>'
        '<th style="padding:9px 8px;border-bottom:1px solid #e5e7eb;text-align:right;'
        'font-family:Arial,Helvetica,sans-serif;font-size:9px;line-height:13px;'
        'color:#6b7280;text-transform:uppercase;letter-spacing:0.06em;">Installed Qty</th>'
        '<th style="padding:9px 8px;border-bottom:1px solid #e5e7eb;text-align:right;'
        'font-family:Arial,Helvetica,sans-serif;font-size:9px;line-height:13px;'
        'color:#6b7280;text-transform:uppercase;letter-spacing:0.06em;">Variance</th>'
        '<th style="padding:9px 8px;border-bottom:1px solid #e5e7eb;text-align:center;'
        'font-family:Arial,Helvetica,sans-serif;font-size:9px;line-height:13px;'
        'color:#6b7280;text-transform:uppercase;letter-spacing:0.06em;">Completion</th>'
        '<th style="padding:9px 8px;border-bottom:1px solid #e5e7eb;text-align:center;'
        'font-family:Arial,Helvetica,sans-serif;font-size:9px;line-height:13px;'
        'color:#6b7280;text-transform:uppercase;letter-spacing:0.06em;">Status</th>'
        "</tr>"
        "</thead>"
        "<tbody>"
        + _build_quantity_rows(alerts)
        + "</tbody></table></div>"
    )

    footer = (
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
        'style="width:100%;border-collapse:collapse;border-top:1px solid #e5e7eb;margin-top:6px;">'
        "<tr>"
        '<td style="padding:15px 0 0 0;font-family:Arial,Helvetica,sans-serif;'
        'font-size:11px;line-height:17px;color:#6b7280;">'
        '<strong style="color:#374151;">Construction Operations API</strong><br>'
        f"Automated quantity tracking alert<br>Generated {html.escape(now_str)}"
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
        'letter-spacing:-0.05em;margin:0 0 9px 0;">Quantity Tracking</div>'
        f'<div style="font-size:16px;line-height:23px;color:#ecfeff;font-weight:700;margin:0;">'
        f"{html.escape(period_start)} to {html.escape(period_end)}</div>"
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
        f'Tracking period: <strong style="color:#111827;">{html.escape(period_start)} to {html.escape(period_end)}</strong>'
        f' &nbsp;|&nbsp; Total alerts: <strong style="color:#111827;">{total_alerts}</strong>'
        "</div>"
        + summary_cards
        + action_box
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

    return subject, html_body
