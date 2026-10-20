"""
schedule_monitoring/email_template.py
──────────────────────────────────────
Builds the HTML email for weekly schedule monitoring reports.
Follows the same style as budget_email_template.py.
"""
from __future__ import annotations

import html
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from schedule_monitoring.analyzer import ScheduleAlert, ScheduleReport

logger = logging.getLogger(__name__)

_MT = ZoneInfo("America/Denver")

_STATUS_COLORS = {
    "CRITICAL":  ("#b91c1c", "#fde8e8"),
    "WARNING":   ("#92400e", "#fef3c7"),
    "ON_TRACK":  ("#15803d", "#dcfce7"),
}
_SEV_COLORS = {
    "critical": ("#b91c1c", "#fde8e8"),
    "warning":  ("#92400e", "#fef3c7"),
}


def build_schedule_email(
    report: ScheduleReport,
    comments: str = "",
) -> tuple[str, str]:
    """
    Build (subject, html_body) for a schedule monitoring report email.
    """
    now_str = datetime.now(_MT).strftime("%Y-%m-%d %H:%M %Z")

    # Subject
    if report.critical_count > 0:
        subject = (
            f"CRITICAL – Schedule Alert: {report.critical_count} critical issue(s) "
            f"across {report.jobs_with_critical} job(s) | "
            f"{report.period_start} → {report.period_end}"
        )
    elif report.warning_count > 0:
        subject = (
            f"Schedule Warning: {report.warning_count} warning(s) | "
            f"{report.period_start} → {report.period_end}"
        )
    else:
        subject = (
            f"Schedule Report: All {report.total_jobs} job(s) on track | "
            f"{report.period_start} → {report.period_end}"
        )

    # ── Summary bar ───────────────────────────────────────────────────────────
    summary_bar = f"""
    <table cellpadding="0" cellspacing="0" border="0"
           style="width:100%;max-width:700px;border-collapse:collapse;margin:0 0 20px 0;">
      <tr>
        <td style="padding:12px 16px;background:#fde8e8;color:#b91c1c;font-weight:bold;
                   font-family:Arial,sans-serif;font-size:14px;text-align:center;">
          Critical: {report.critical_count}
        </td>
        <td style="padding:12px 16px;background:#fef3c7;color:#92400e;font-weight:bold;
                   font-family:Arial,sans-serif;font-size:14px;text-align:center;">
          Warnings: {report.warning_count}
        </td>
        <td style="padding:12px 16px;background:#dcfce7;color:#15803d;font-weight:bold;
                   font-family:Arial,sans-serif;font-size:14px;text-align:center;">
          Jobs On Track: {report.total_jobs - report.jobs_with_critical - report.jobs_with_warnings}
        </td>
        <td style="padding:12px 16px;background:#f3f4f6;color:#374151;font-weight:bold;
                   font-family:Arial,sans-serif;font-size:14px;text-align:center;">
          Total Jobs: {report.total_jobs}
        </td>
      </tr>
    </table>
    """

    # ── Job summary table ─────────────────────────────────────────────────────
    job_rows = ""
    for j in sorted(report.job_summaries,
                    key=lambda x: (0 if x["status"] == "CRITICAL" else
                                   1 if x["status"] == "WARNING" else 2)):
        fc, bg = _STATUS_COLORS.get(j["status"], ("#374151", "#f3f4f6"))
        est_end = j.get("estimated_end_date") or "—"
        last_tc = j.get("last_timecard_date") or "—"
        job_rows += f"""
        <tr>
          <td style="padding:8px 10px;border:1px solid #e5e7eb;font-family:Arial,sans-serif;font-size:13px;">
            {html.escape(j['job_code'])}</td>
          <td style="padding:8px 10px;border:1px solid #e5e7eb;font-family:Arial,sans-serif;font-size:13px;">
            {html.escape(j['job_name'])}</td>
          <td style="padding:8px 10px;border:1px solid #e5e7eb;background:{bg};color:{fc};
                     font-weight:bold;font-family:Arial,sans-serif;font-size:13px;text-align:center;">
            {html.escape(j['status'])}</td>
          <td style="padding:8px 10px;border:1px solid #e5e7eb;font-family:Arial,sans-serif;font-size:13px;text-align:right;">
            {j['cost_pct']:.1f}%</td>
          <td style="padding:8px 10px;border:1px solid #e5e7eb;font-family:Arial,sans-serif;font-size:13px;text-align:right;">
            {j['quantity_pct']:.1f}%</td>
          <td style="padding:8px 10px;border:1px solid #e5e7eb;font-family:Arial,sans-serif;font-size:13px;text-align:right;">
            ${j['total_actual_cost']:,.0f}</td>
          <td style="padding:8px 10px;border:1px solid #e5e7eb;font-family:Arial,sans-serif;font-size:13px;text-align:center;">
            {html.escape(est_end)}</td>
          <td style="padding:8px 10px;border:1px solid #e5e7eb;font-family:Arial,sans-serif;font-size:13px;text-align:center;">
            {html.escape(last_tc)}</td>
          <td style="padding:8px 10px;border:1px solid #e5e7eb;font-family:Arial,sans-serif;font-size:13px;text-align:center;color:#b91c1c;">
            {j['critical_alerts']}</td>
          <td style="padding:8px 10px;border:1px solid #e5e7eb;font-family:Arial,sans-serif;font-size:13px;text-align:center;color:#92400e;">
            {j['warning_alerts']}</td>
        </tr>"""

    job_table = f"""
    <h2 style="font-size:16px;color:#111827;font-family:Arial,sans-serif;margin:24px 0 8px 0;">
      Job Summary
    </h2>
    <table cellpadding="0" cellspacing="0" border="0"
           style="width:100%;max-width:900px;border-collapse:collapse;margin-bottom:24px;">
      <thead>
        <tr style="background:#f3f4f6;">
          <th style="padding:8px 10px;border:1px solid #e5e7eb;text-align:left;
                     font-family:Arial,sans-serif;font-size:12px;color:#374151;">Job Code</th>
          <th style="padding:8px 10px;border:1px solid #e5e7eb;text-align:left;
                     font-family:Arial,sans-serif;font-size:12px;color:#374151;">Job Name</th>
          <th style="padding:8px 10px;border:1px solid #e5e7eb;text-align:center;
                     font-family:Arial,sans-serif;font-size:12px;color:#374151;">Status</th>
          <th style="padding:8px 10px;border:1px solid #e5e7eb;text-align:right;
                     font-family:Arial,sans-serif;font-size:12px;color:#374151;">Cost %</th>
          <th style="padding:8px 10px;border:1px solid #e5e7eb;text-align:right;
                     font-family:Arial,sans-serif;font-size:12px;color:#374151;">Qty %</th>
          <th style="padding:8px 10px;border:1px solid #e5e7eb;text-align:right;
                     font-family:Arial,sans-serif;font-size:12px;color:#374151;">Actual Cost</th>
          <th style="padding:8px 10px;border:1px solid #e5e7eb;text-align:center;
                     font-family:Arial,sans-serif;font-size:12px;color:#374151;">Est. End</th>
          <th style="padding:8px 10px;border:1px solid #e5e7eb;text-align:center;
                     font-family:Arial,sans-serif;font-size:12px;color:#374151;">Last TC</th>
          <th style="padding:8px 10px;border:1px solid #e5e7eb;text-align:center;
                     font-family:Arial,sans-serif;font-size:12px;color:#b91c1c;">Critical</th>
          <th style="padding:8px 10px;border:1px solid #e5e7eb;text-align:center;
                     font-family:Arial,sans-serif;font-size:12px;color:#92400e;">Warnings</th>
        </tr>
      </thead>
      <tbody>{job_rows}</tbody>
    </table>
    """

    # ── Alert detail section ──────────────────────────────────────────────────
    critical_alerts = [a for a in report.alerts if a.severity == "critical"]
    warning_alerts  = [a for a in report.alerts if a.severity == "warning"]

    def _alert_rows(alerts: list[ScheduleAlert], color: str) -> str:
        rows = ""
        for i, a in enumerate(alerts, 1):
            cc_label = (
                f"{html.escape(a.cost_code_name)}" if a.cost_code_name else "—"
            )
            rows += f"""
            <tr style="background:{'#fff8f8' if color == '#b91c1c' else '#fffbf0'};">
              <td style="padding:8px 10px;border:1px solid #e5e7eb;
                         font-family:Arial,sans-serif;font-size:12px;text-align:center;
                         color:{color};font-weight:bold;">{i}</td>
              <td style="padding:8px 10px;border:1px solid #e5e7eb;
                         font-family:Arial,sans-serif;font-size:12px;">
                {html.escape(a.job_code)} – {html.escape(a.job_name)}</td>
              <td style="padding:8px 10px;border:1px solid #e5e7eb;
                         font-family:Arial,sans-serif;font-size:12px;">{cc_label}</td>
              <td style="padding:8px 10px;border:1px solid #e5e7eb;
                         font-family:Arial,sans-serif;font-size:12px;">{html.escape(a.alert_type)}</td>
              <td style="padding:8px 10px;border:1px solid #e5e7eb;
                         font-family:Arial,sans-serif;font-size:12px;">{html.escape(a.message)}</td>
            </tr>"""
        return rows

    def _alert_table(title: str, alerts: list[ScheduleAlert], color: str) -> str:
        if not alerts:
            return ""
        return f"""
        <h2 style="font-size:16px;color:{color};font-family:Arial,sans-serif;margin:24px 0 8px 0;">
          {html.escape(title)} ({len(alerts)})
        </h2>
        <table cellpadding="0" cellspacing="0" border="0"
               style="width:100%;max-width:900px;border-collapse:collapse;margin-bottom:20px;">
          <thead>
            <tr style="background:#f3f4f6;">
              <th style="padding:8px 10px;border:1px solid #e5e7eb;font-family:Arial,sans-serif;font-size:12px;">#</th>
              <th style="padding:8px 10px;border:1px solid #e5e7eb;font-family:Arial,sans-serif;font-size:12px;">Job</th>
              <th style="padding:8px 10px;border:1px solid #e5e7eb;font-family:Arial,sans-serif;font-size:12px;">Cost Code</th>
              <th style="padding:8px 10px;border:1px solid #e5e7eb;font-family:Arial,sans-serif;font-size:12px;">Type</th>
              <th style="padding:8px 10px;border:1px solid #e5e7eb;font-family:Arial,sans-serif;font-size:12px;">Message</th>
            </tr>
          </thead>
          <tbody>{_alert_rows(alerts, color)}</tbody>
        </table>"""

    alert_sections = (
        _alert_table("🚨 Critical Alerts", critical_alerts, "#b91c1c")
        + _alert_table("⚠️ Warnings", warning_alerts, "#92400e")
    )

    # ── Comments ──────────────────────────────────────────────────────────────
    comments_html = ""
    if comments:
        comments_html = f"""
        <div style="margin:16px 0;padding:12px;background:#f9fafb;border-left:4px solid #3b82f6;">
          <p style="margin:0 0 4px 0;font-weight:bold;font-size:13px;color:#374151;font-family:Arial,sans-serif;">
            Comments:</p>
          <p style="margin:0;font-size:13px;color:#1f2937;font-family:Arial,sans-serif;">
            {html.escape(comments)}</p>
        </div>"""

    # ── Baseline note ─────────────────────────────────────────────────────────
    baseline_note = """
    <div style="margin:16px 0;padding:10px 14px;background:#eff6ff;border-left:4px solid #3b82f6;
                font-family:Arial,sans-serif;font-size:12px;color:#1e40af;">
      <strong>Note:</strong> Analysis is based on actuals only (HCSS + DB).
      Planned baseline from the 6-week schedule (Google Drive) is not yet connected.
      When baseline is available, additional checks will run:
      planned vs actual quantities per week, out-of-sequence detection, and
      critical path impact analysis.
    </div>"""

    # ── Footer ────────────────────────────────────────────────────────────────
    footer = f"""
    <p style="margin:24px 0 0 0;padding-top:16px;border-top:1px solid #e5e7eb;
              font-size:12px;color:#6b7280;font-family:Arial,sans-serif;">
      Sent by Construction Operations API — {html.escape(now_str)}
    </p>"""

    html_body = f"""
    <div style="font-family:Arial,Helvetica,sans-serif;color:#111827;max-width:900px;">
      <h1 style="margin:0 0 6px 0;font-size:22px;color:#111827;">
        Schedule Monitoring Report
      </h1>
      <p style="margin:0 0 16px 0;font-size:14px;color:#374151;">
        Period: <strong>{html.escape(report.period_start)} → {html.escape(report.period_end)}</strong>
        &nbsp;·&nbsp; Generated: {html.escape(report.generated_at[:19].replace('T',' '))}
      </p>
      {summary_bar}
      {baseline_note}
      {job_table}
      {alert_sections}
      {comments_html}
      {footer}
    </div>"""

    logger.info(
        "Schedule email built: critical=%d warnings=%d jobs=%d",
        report.critical_count, report.warning_count, report.total_jobs,
    )
    return subject, html_body
