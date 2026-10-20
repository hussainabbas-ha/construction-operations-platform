"""
postmortem/email_template.py
─────────────────────────────
HTML email template for the Postmortem Analysis report.

Follows the same pattern as notifications/budget_email_template.py.
Returns (subject, html_body) tuple consumed by the existing SMTP sender.
"""
from __future__ import annotations

from postmortem.schemas import PostmortemDetailResponse


# ── Colour palette (matches existing email templates) ────────────────────────
_COLOUR_ON_TRACK    = "#16a34a"   # green
_COLOUR_AT_RISK     = "#d97706"   # amber
_COLOUR_OVER_BUDGET = "#dc2626"   # red
_COLOUR_NEUTRAL     = "#374151"   # dark grey
_COLOUR_BG_HEADER   = "#1e3a5f"   # dark navy (matches budget template)
_COLOUR_BG_ROW_ALT  = "#f9fafb"   # light grey alternate row


def _risk_colour(risk: str) -> str:
    return {
        "ON_TRACK":    _COLOUR_ON_TRACK,
        "AT_RISK":     _COLOUR_AT_RISK,
        "OVER_BUDGET": _COLOUR_OVER_BUDGET,
    }.get(risk, _COLOUR_NEUTRAL)


def _fmt_currency(value: float) -> str:
    return f"${value:,.2f}"


def _fmt_pct(value: float | None) -> str:
    if value is None:
        return "N/A"
    return f"{value:.1f}%"


def _kpi_card(label: str, value: str, colour: str = _COLOUR_NEUTRAL) -> str:
    return f"""
    <td style="padding:12px 16px;text-align:center;background:#fff;
               border:1px solid #e5e7eb;border-radius:6px;min-width:130px;">
      <div style="font-size:11px;color:#6b7280;text-transform:uppercase;
                  letter-spacing:.05em;margin-bottom:4px;">{label}</div>
      <div style="font-size:20px;font-weight:700;color:{colour};">{value}</div>
    </td>"""


def _section_header(title: str) -> str:
    return f"""
    <tr>
      <td colspan="99" style="padding:16px 0 6px 0;">
        <div style="font-size:14px;font-weight:700;color:{_COLOUR_BG_HEADER};
                    border-bottom:2px solid {_COLOUR_BG_HEADER};padding-bottom:4px;">
          {title}
        </div>
      </td>
    </tr>"""


def _th(text: str) -> str:
    return (
        f'<th style="padding:8px 12px;text-align:left;background:{_COLOUR_BG_HEADER};'
        f'color:#fff;font-size:11px;font-weight:600;white-space:nowrap;">{text}</th>'
    )


def _td(text: str, bold: bool = False, colour: str = _COLOUR_NEUTRAL, align: str = "left") -> str:
    weight = "700" if bold else "400"
    return (
        f'<td style="padding:7px 12px;font-size:12px;color:{colour};'
        f'font-weight:{weight};text-align:{align};border-bottom:1px solid #e5e7eb;">'
        f'{text}</td>'
    )


def _build_cost_table(detail: PostmortemDetailResponse) -> str:
    if not detail.cost_codes:
        return "<p style='color:#6b7280;font-size:12px;'>No cost data available.</p>"

    rows_html = ""
    for i, cc in enumerate(detail.cost_codes):
        bg = _COLOUR_BG_ROW_ALT if i % 2 == 0 else "#fff"
        colour = _risk_colour(cc.status)
        rows_html += f"""
        <tr style="background:{bg};">
          {_td(cc.cost_code, bold=True)}
          {_td(cc.cost_code_description)}
          {_td(_fmt_currency(cc.expected_budget), align="right")}
          {_td(_fmt_currency(cc.actual_cost), align="right")}
          {_td(_fmt_pct(cc.utilization_pct), align="right")}
          {_td(_fmt_currency(cc.loss_amount), align="right",
               colour=_COLOUR_OVER_BUDGET if cc.loss_amount > 0 else _COLOUR_NEUTRAL)}
          {_td(cc.status, colour=colour, bold=True)}
        </tr>"""

    return f"""
    <table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;margin-top:8px;">
      <thead><tr>
        {_th("Cost Code")}{_th("Description")}{_th("Budget")}{_th("Actual")}
        {_th("Utilisation")}{_th("Loss")}{_th("Status")}
      </tr></thead>
      <tbody>{rows_html}</tbody>
    </table>"""


def _build_missed_cost_table(detail: PostmortemDetailResponse) -> str:
    if not detail.missed_costs:
        return "<p style='color:#16a34a;font-size:12px;'>No missed costs detected.</p>"

    rows_html = ""
    for i, mc in enumerate(detail.missed_costs):
        bg = _COLOUR_BG_ROW_ALT if i % 2 == 0 else "#fff"
        rows_html += f"""
        <tr style="background:{bg};">
          {_td(mc.cost_code, bold=True)}
          {_td(mc.description)}
          {_td(_fmt_pct(mc.qty_percent_complete) + " qty done", align="right")}
          {_td(_fmt_pct(mc.cost_utilization_pct) + " cost used", align="right")}
          {_td(_fmt_currency(mc.estimated_missed_value), align="right",
               colour=_COLOUR_OVER_BUDGET, bold=True)}
          {_td(mc.source.replace("_", " ").title())}
        </tr>"""

    return f"""
    <table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;margin-top:8px;">
      <thead><tr>
        {_th("Cost Code")}{_th("Description")}{_th("Qty Complete")}
        {_th("Cost Used")}{_th("Est. Missed")}{_th("Source")}
      </tr></thead>
      <tbody>{rows_html}</tbody>
    </table>"""


def _build_timelog_block(detail: PostmortemDetailResponse) -> str:
    tl = detail.timelog
    if tl is None:
        return "<p style='color:#6b7280;font-size:12px;'>No timelog data available for this project.</p>"

    score_colour = (
        _COLOUR_ON_TRACK if tl.quality_score_pct >= 90
        else _COLOUR_AT_RISK if tl.quality_score_pct >= 70
        else _COLOUR_OVER_BUDGET
    )
    reasons_html = (
        "<ul style='margin:4px 0 0 16px;padding:0;font-size:12px;color:#374151;'>"
        + "".join(f"<li>{r}</li>" for r in tl.rejection_reasons[:5])
        + "</ul>"
    ) if tl.rejection_reasons else ""

    return f"""
    <table width="100%" cellpadding="0" cellspacing="4" style="margin-top:8px;">
      <tr>
        <td style="padding:8px 12px;background:#f9fafb;border:1px solid #e5e7eb;border-radius:4px;">
          <span style="font-size:11px;color:#6b7280;">Total Timecards</span><br>
          <span style="font-size:18px;font-weight:700;color:{_COLOUR_NEUTRAL};">{tl.total_timecards}</span>
        </td>
        <td style="padding:8px 12px;background:#f9fafb;border:1px solid #e5e7eb;border-radius:4px;">
          <span style="font-size:11px;color:#6b7280;">Approved</span><br>
          <span style="font-size:18px;font-weight:700;color:{_COLOUR_ON_TRACK};">{tl.approved}</span>
        </td>
        <td style="padding:8px 12px;background:#f9fafb;border:1px solid #e5e7eb;border-radius:4px;">
          <span style="font-size:11px;color:#6b7280;">Flagged</span><br>
          <span style="font-size:18px;font-weight:700;color:{_COLOUR_AT_RISK};">{tl.flagged}</span>
        </td>
        <td style="padding:8px 12px;background:#f9fafb;border:1px solid #e5e7eb;border-radius:4px;">
          <span style="font-size:11px;color:#6b7280;">Rejected</span><br>
          <span style="font-size:18px;font-weight:700;color:{_COLOUR_OVER_BUDGET};">{tl.rejected}</span>
        </td>
        <td style="padding:8px 12px;background:#f9fafb;border:1px solid #e5e7eb;border-radius:4px;">
          <span style="font-size:11px;color:#6b7280;">Quality Score</span><br>
          <span style="font-size:18px;font-weight:700;color:{score_colour};">{tl.quality_score_pct:.1f}%</span>
        </td>
      </tr>
    </table>
    {"<p style='font-size:12px;color:#374151;margin-top:8px;'><strong>Top rejection reasons:</strong></p>" + reasons_html if reasons_html else ""}"""


def build_postmortem_email_body(
    detail: PostmortemDetailResponse,
    comments: str = "",
    triggered_by: str = "Manual",
) -> tuple[str, str]:
    """
    Build the subject line and HTML body for a postmortem report email.

    Args:
        detail:       Full PostmortemDetailResponse for the project.
        comments:     Optional user comments to include.
        triggered_by: Who triggered the report (e.g. "Manual", "PM Request").

    Returns:
        (subject, html_body) tuple.
    """
    risk_colour = _risk_colour(detail.overall_risk)
    proj_label  = detail.projection.tracking_month if detail.projection else "N/A"

    subject = (
        f"Postmortem Report — {detail.job_code} | {detail.job_name} "
        f"[{detail.overall_risk}]"
    )

    # ── KPI cards row ──────────────────────────────────────────────────────
    kpi_row = (
        _kpi_card("Overall Risk",    detail.overall_risk,                    risk_colour)
        + _kpi_card("Total Budget",  _fmt_currency(detail.total_expected_budget))
        + _kpi_card("Actual Cost",   _fmt_currency(detail.total_actual_cost))
        + _kpi_card("Variance",      _fmt_currency(detail.total_variance),
                    _COLOUR_ON_TRACK if detail.total_variance >= 0 else _COLOUR_OVER_BUDGET)
        + _kpi_card("Loss",          _fmt_currency(detail.total_loss_amount),
                    _COLOUR_OVER_BUDGET if detail.total_loss_amount > 0 else _COLOUR_ON_TRACK)
        + _kpi_card("Missed Cost",   _fmt_currency(detail.estimated_missed_cost_total),
                    _COLOUR_AT_RISK if detail.estimated_missed_cost_total > 0 else _COLOUR_ON_TRACK)
    )

    # ── Projection block ───────────────────────────────────────────────────
    proj_html = ""
    if detail.projection:
        p = detail.projection
        proj_html = f"""
        <tr><td style="padding:8px 0;">
          <table width="100%" cellpadding="0" cellspacing="4">
            <tr>
              <td style="padding:8px 12px;background:#f9fafb;border:1px solid #e5e7eb;border-radius:4px;">
                <span style="font-size:11px;color:#6b7280;">EAC (Projected Final Cost)</span><br>
                <span style="font-size:16px;font-weight:700;color:{_COLOUR_NEUTRAL};">{_fmt_currency(p.projected_final_cost)}</span>
              </td>
              <td style="padding:8px 12px;background:#f9fafb;border:1px solid #e5e7eb;border-radius:4px;">
                <span style="font-size:11px;color:#6b7280;">% Complete</span><br>
                <span style="font-size:16px;font-weight:700;color:{_COLOUR_NEUTRAL};">{p.percent_complete:.1f}%</span>
              </td>
              <td style="padding:8px 12px;background:#f9fafb;border:1px solid #e5e7eb;border-radius:4px;">
                <span style="font-size:11px;color:#6b7280;">Billed to Date</span><br>
                <span style="font-size:16px;font-weight:700;color:{_COLOUR_NEUTRAL};">{_fmt_currency(p.billed_to_date)}</span>
              </td>
              <td style="padding:8px 12px;background:#f9fafb;border:1px solid #e5e7eb;border-radius:4px;">
                <span style="font-size:11px;color:#6b7280;">Billing Variance</span><br>
                <span style="font-size:16px;font-weight:700;
                  color:{'#dc2626' if p.billing_variance < 0 else '#16a34a'};">
                  {_fmt_currency(p.billing_variance)}
                </span>
              </td>
              <td style="padding:8px 12px;background:#f9fafb;border:1px solid #e5e7eb;border-radius:4px;">
                <span style="font-size:11px;color:#6b7280;">Est. Completion</span><br>
                <span style="font-size:16px;font-weight:700;color:{_COLOUR_NEUTRAL};">{p.estimated_completion_month or "N/A"}</span>
              </td>
            </tr>
          </table>
        </td></tr>"""

    # ── Comments block ─────────────────────────────────────────────────────
    comments_html = ""
    if comments.strip():
        comments_html = f"""
        <tr><td style="padding:12px 0 0 0;">
          <div style="background:#fffbeb;border-left:4px solid #d97706;padding:10px 14px;border-radius:4px;">
            <strong style="font-size:12px;color:#92400e;">Comments:</strong>
            <p style="margin:4px 0 0 0;font-size:12px;color:#374151;">{comments}</p>
          </div>
        </td></tr>"""

    html_body = f"""
<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0"></head>
<body style="margin:0;padding:0;background:#f3f4f6;font-family:Arial,Helvetica,sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#f3f4f6;padding:24px 0;">
  <tr><td align="center">
    <table width="680" cellpadding="0" cellspacing="0"
           style="background:#fff;border-radius:8px;overflow:hidden;
                  box-shadow:0 1px 3px rgba(0,0,0,.12);">

      <!-- Header -->
      <tr>
        <td style="background:{_COLOUR_BG_HEADER};padding:20px 28px;">
          <div style="font-size:11px;color:#93c5fd;text-transform:uppercase;letter-spacing:.08em;">
            Construction Operations API — Postmortem Analysis
          </div>
          <div style="font-size:22px;font-weight:700;color:#fff;margin-top:4px;">
            {detail.job_code} — {detail.job_name}
          </div>
          <div style="font-size:12px;color:#bfdbfe;margin-top:4px;">
            Business Unit: {detail.business_unit} &nbsp;|&nbsp;
            Latest Projection: {proj_label} &nbsp;|&nbsp;
            Triggered by: {triggered_by}
          </div>
        </td>
      </tr>

      <!-- Body -->
      <tr><td style="padding:24px 28px;">
        <table width="100%" cellpadding="0" cellspacing="0">

          <!-- KPI Cards -->
          <tr><td style="padding-bottom:20px;">
            <table cellpadding="6" cellspacing="8"><tr>{kpi_row}</tr></table>
          </td></tr>

          <!-- Projection Summary -->
          {"<tr><td><div style='font-size:14px;font-weight:700;color:" + _COLOUR_BG_HEADER + ";border-bottom:2px solid " + _COLOUR_BG_HEADER + ";padding-bottom:4px;margin-bottom:8px;'>Projection Summary</div></td></tr>" + proj_html if detail.projection else ""}

          <!-- Cost vs Budget -->
          <tr><td style="padding-top:20px;">
            <div style="font-size:14px;font-weight:700;color:{_COLOUR_BG_HEADER};
                        border-bottom:2px solid {_COLOUR_BG_HEADER};padding-bottom:4px;margin-bottom:8px;">
              Cost vs Budget
            </div>
            {_build_cost_table(detail)}
          </td></tr>

          <!-- Missed Costs -->
          <tr><td style="padding-top:20px;">
            <div style="font-size:14px;font-weight:700;color:{_COLOUR_BG_HEADER};
                        border-bottom:2px solid {_COLOUR_BG_HEADER};padding-bottom:4px;margin-bottom:8px;">
              Missed / Unrecorded Costs
            </div>
            {_build_missed_cost_table(detail)}
          </td></tr>

          <!-- Time Log Quality -->
          <tr><td style="padding-top:20px;">
            <div style="font-size:14px;font-weight:700;color:{_COLOUR_BG_HEADER};
                        border-bottom:2px solid {_COLOUR_BG_HEADER};padding-bottom:4px;margin-bottom:8px;">
              Time Log Quality
            </div>
            {_build_timelog_block(detail)}
          </td></tr>

          <!-- Deferred sections notice -->
          <tr><td style="padding-top:20px;">
            <div style="background:#f0f9ff;border:1px solid #bae6fd;border-radius:6px;padding:12px 16px;">
              <p style="margin:0;font-size:12px;color:#0369a1;">
                <strong>Pending sections:</strong>
                Change Order Analysis and Schedule Performance will be included
                once the Billing Draft and Schedule Monitoring modules are complete.
              </p>
            </div>
          </td></tr>

          {comments_html}

        </table>
      </td></tr>

      <!-- Footer -->
      <tr>
        <td style="background:#f9fafb;padding:14px 28px;border-top:1px solid #e5e7eb;">
          <p style="margin:0;font-size:11px;color:#9ca3af;">
            This report was generated automatically by the Construction Operations API system.
            Data sourced from HCSS HeavyJob via the Budget, Quantity, Projection, and
            Time Log Verification modules.
          </p>
        </td>
      </tr>

    </table>
  </td></tr>
</table>
</body>
</html>"""

    return subject, html_body
