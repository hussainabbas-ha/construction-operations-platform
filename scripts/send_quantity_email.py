"""
Manual quantity tracking email sender script.

Usage:
    python scripts/send_quantity_email.py --date 2026-05-01 --to email@example.com
    python scripts/send_quantity_email.py --date 2026-05-01 --to a@b.com,c@d.com
    python scripts/send_quantity_email.py --start-date 2026-04-01 --end-date 2026-04-30 --to a@b.com
"""
from __future__ import annotations

import argparse
import html as html_mod
import logging
import os
import sys
from datetime import date, datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

from notifications.individual_email_sender import send_individual_email
from quantity_tracking.tracker import track_quantities

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)-8s] %(name)s: %(message)s",
)
logger = logging.getLogger("send_quantity_email")

_MT = ZoneInfo("America/Denver")


def _status_style(s):
    if s == "OVER_RISK":
        return "background:#fde8e8;color:#b91c1c;font-weight:bold;"
    if s == "NEAR_COMPLETION":
        return "background:#ffedd5;color:#c2410c;font-weight:bold;"
    return "background:#dcfce7;color:#15803d;"


def build_quantity_email(results, period_start, period_end):
    now_str      = datetime.now(_MT).strftime("%Y-%m-%d %H:%M %Z")
    total        = len(results)
    on_track     = sum(1 for r in results if r.status == "ON_TRACK")
    near         = sum(1 for r in results if r.status == "NEAR_COMPLETION")
    over         = sum(1 for r in results if r.status == "OVER_RISK")
    period_label = period_start if period_start == period_end else f"{period_start} to {period_end}"
    subject      = f"Quantity Tracking Report | {period_label} | {near + over} item(s) need attention"

    rows_html = ""
    for r in results:
        rows_html += (
            "<tr>"
            + "<td style='padding:8px 10px;border:1px solid #e5e7eb;font-size:13px;'>" + html_mod.escape(r.cost_code) + "</td>"
            + "<td style='padding:8px 10px;border:1px solid #e5e7eb;font-size:13px;'>" + html_mod.escape(r.description) + "</td>"
            + "<td style='padding:8px 10px;border:1px solid #e5e7eb;font-size:13px;text-align:right;'>" + f"{r.planned_quantity:,.2f} {html_mod.escape(r.unit)}" + "</td>"
            + "<td style='padding:8px 10px;border:1px solid #e5e7eb;font-size:13px;text-align:right;'>" + f"{r.installed_quantity:,.2f} {html_mod.escape(r.unit)}" + "</td>"
            + "<td style='padding:8px 10px;border:1px solid #e5e7eb;font-size:13px;text-align:right;'>" + f"{r.remaining_quantity:,.2f} {html_mod.escape(r.unit)}" + "</td>"
            + "<td style='padding:8px 10px;border:1px solid #e5e7eb;font-size:13px;text-align:center;'>" + f"{r.percent_complete:.1f}%" + "</td>"
            + "<td style='padding:8px 10px;border:1px solid #e5e7eb;font-size:12px;text-align:center;" + _status_style(r.status) + "'>" + html_mod.escape(r.status.replace("_", " ")) + "</td>"
            + "<td style='padding:8px 10px;border:1px solid #e5e7eb;font-size:13px;'>" + html_mod.escape(r.cost_type.replace("_", " ").title()) + "</td>"
            + "</tr>"
        )

    summary_cards = (
        "<table role='presentation' cellpadding='0' cellspacing='0' border='0'"
        " style='width:100%;margin-bottom:24px;border-collapse:collapse;'><tr>"
        + "<td style='padding:16px;border:1px solid #e5e7eb;text-align:center;width:25%;'>"
        + f"<div style='font-size:24px;font-weight:bold;color:#111827;'>{total}</div>"
        + "<div style='font-size:12px;color:#6b7280;margin-top:4px;'>Total Items</div></td>"
        + "<td style='padding:16px;border:1px solid #e5e7eb;text-align:center;width:25%;'>"
        + f"<div style='font-size:24px;font-weight:bold;color:#15803d;'>{on_track}</div>"
        + "<div style='font-size:12px;color:#6b7280;margin-top:4px;'>On Track</div></td>"
        + "<td style='padding:16px;border:1px solid #e5e7eb;text-align:center;width:25%;'>"
        + f"<div style='font-size:24px;font-weight:bold;color:#c2410c;'>{near}</div>"
        + "<div style='font-size:12px;color:#6b7280;margin-top:4px;'>Near Completion (75%+)</div></td>"
        + "<td style='padding:16px;border:1px solid #e5e7eb;text-align:center;width:25%;'>"
        + f"<div style='font-size:24px;font-weight:bold;color:#b91c1c;'>{over}</div>"
        + "<div style='font-size:12px;color:#6b7280;margin-top:4px;'>Over / At Risk</div></td>"
        + "</tr></table>"
    )

    table_header = (
        "<table role='presentation' cellpadding='0' cellspacing='0' border='0'"
        " style='width:100%;border-collapse:collapse;margin-bottom:16px;'>"
        "<thead><tr style='background:#f3f4f6;'>"
        "<th style='padding:8px 10px;border:1px solid #e5e7eb;text-align:left;font-size:12px;color:#374151;'>Cost Code</th>"
        "<th style='padding:8px 10px;border:1px solid #e5e7eb;text-align:left;font-size:12px;color:#374151;'>Description</th>"
        "<th style='padding:8px 10px;border:1px solid #e5e7eb;text-align:right;font-size:12px;color:#374151;'>Planned Qty</th>"
        "<th style='padding:8px 10px;border:1px solid #e5e7eb;text-align:right;font-size:12px;color:#374151;'>Installed Qty</th>"
        "<th style='padding:8px 10px;border:1px solid #e5e7eb;text-align:right;font-size:12px;color:#374151;'>Remaining Qty</th>"
        "<th style='padding:8px 10px;border:1px solid #e5e7eb;text-align:center;font-size:12px;color:#374151;'>% Complete</th>"
        "<th style='padding:8px 10px;border:1px solid #e5e7eb;text-align:center;font-size:12px;color:#374151;'>Status</th>"
        "<th style='padding:8px 10px;border:1px solid #e5e7eb;text-align:left;font-size:12px;color:#374151;'>Type</th>"
        "</tr></thead>"
        f"<tbody>{rows_html}</tbody></table>"
    )

    footer = (
        "<p style='margin:24px 0 0 0;padding-top:16px;border-top:1px solid #e5e7eb;"
        "font-size:12px;color:#6b7280;font-family:Arial,sans-serif;'>"
        f"Sent by Gould APM — Quantity Tracking System — {html_mod.escape(now_str)}"
        "</p>"
    )

    html_body = (
        "<div style='font-family:Arial,Helvetica,sans-serif;color:#111827;max-width:900px;'>"
        "<h2 style='margin:0 0 8px 0;font-size:18px;color:#111827;'>Quantity Tracking Report</h2>"
        f"<p style='margin:0 0 16px 0;font-size:14px;color:#374151;'>Period: <strong>{html_mod.escape(period_label)}</strong></p>"
        + summary_cards
        + table_header
        + footer
        + "</div>"
    )

    return subject, html_body


def main():
    parser = argparse.ArgumentParser(
        description="Send quantity tracking email manually."
    )
    parser.add_argument("--date", type=str, default=None,
        help="Single date YYYY-MM-DD")
    parser.add_argument("--start-date", type=str, default=None,
        help="Range start YYYY-MM-DD (use with --end-date)")
    parser.add_argument("--end-date", type=str, default=None,
        help="Range end YYYY-MM-DD (use with --start-date)")
    parser.add_argument("--to", type=str, required=True,
        help="Comma-separated recipient emails e.g. pm@company.com,super@company.com")
    args = parser.parse_args()

    # Resolve dates
    if args.date:
        period_start = period_end = args.date
    elif args.start_date and args.end_date:
        period_start = args.start_date
        period_end   = args.end_date
    else:
        period_start = period_end = str(date.today())
        logger.info("No date provided — defaulting to today: %s", period_start)

    # Parse recipients
    recipients = [r.strip() for r in args.to.split(",") if r.strip()]
    if not recipients:
        logger.error("No valid recipients in --to argument.")
        sys.exit(1)

    logger.info("Fetching quantity data: %s to %s", period_start, period_end)

    try:
        results = track_quantities(start_date=period_start, end_date=period_end)
    except Exception as exc:
        logger.error("Failed to fetch quantity data: %s", exc)
        sys.exit(1)

    if not results:
        logger.warning("No data found for %s to %s — email not sent", period_start, period_end)
        sys.exit(0)

    logger.info("Fetched %d cost code(s)", len(results))
    logger.info("Sending to: %s", ", ".join(recipients))

    subject, html_body = build_quantity_email(results, period_start, period_end)

    try:
        send_individual_email(subject=subject, html_body=html_body, recipients=recipients)
        logger.info("Email sent successfully.")
    except Exception as exc:
        logger.error("Failed to send email: %s", exc)
        sys.exit(1)


if __name__ == "__main__":
    main()