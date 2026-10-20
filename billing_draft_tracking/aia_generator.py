"""
AIA "Application & Certificate for Payment" (G702/G703-style) draft generator.

Hybrid design:
  • Gemini (gemini-3.1-flash-lite) makes the BILLING JUDGMENT per cost code —
    which lines to bill automatically vs hold for manual review — applying the
    draft billing rules, and writes the assumptions/notes narrative.
  • Python does ALL the dollar math (the G702 formulas), so financial totals are
    deterministic and never an LLM arithmetic guess.

Input is the monthly billing-draft rows already stored per (period, job_code).
"""
from __future__ import annotations

import json
import logging
from datetime import date

logger = logging.getLogger(__name__)

_MONTH = ["", "January", "February", "March", "April", "May", "June",
          "July", "August", "September", "October", "November", "December"]

SYSTEM_INSTRUCTION = (
    "You are a senior construction project accountant preparing a DRAFT AIA "
    "G702/G703 Application & Certificate for Payment from a monthly cost-code "
    "report. You are conservative: you only auto-bill work that is clearly "
    "supported by measured direct quantities. You never invent numbers. You "
    "return ONLY valid JSON in the exact schema requested."
)


def _build_prompt(job_code: str, job_description: str, period: str, rows: list[dict]) -> str:
    compact = [
        {
            "cost_code": r["cost_code"],
            "description": r["description"],
            "unit": r["unit"],
            "budgeted_quantity": r["budgeted_quantity"],
            "actual_quantity": r["actual_quantity"],
            "percent_complete": r["percent_complete"],
            "scheduled_value": r["budgeted_all_cost"],
            "earned_value_capped": r["capped_expected_all_cost"],
            "actual_cost": r["actual_all_cost"],
        }
        for r in rows
    ]
    return f"""Prepare the line-item billing DECISIONS for a draft AIA pay application.

Project: job {job_code} — {job_description}
Billing period: {period}

For EACH cost code below decide whether to BILL it automatically this draft or
HOLD it for manual PM review, using these conservative rules:

BILL (billed=true) when ALL of these hold:
  • the unit is a measurable direct quantity (e.g. LF, CY, SF, TON, EA, HR), and
  • actual_quantity > 0 and percent_complete > 0 (work is clearly supported), and
  • it is NOT a lump-sum / administrative / support / oversight / management /
    force-account / mobilization type activity.

HOLD (billed=false) when ANY of these hold (give a short hold_reason):
  • unit is "LS" (lump sum) or blank, or
  • it is admin / management / oversight / safety / support / force-account /
    mobilization / trucking-overhead type work, or
  • actual_quantity is 0 or the quantity is not clearly supported, or
  • reported quantity exceeds the scheduled quantity (note the excess; the earned
    value is already capped at the scheduled value).

Return ONLY this JSON (no prose, no markdown):
{{
  "line_items": [
    {{"cost_code": "<code>", "billed": true|false, "hold_reason": "<short reason or null>"}}
  ],
  "assumptions": ["<short bullet>", "..."]
}}

Include EVERY cost code exactly once in line_items. Keep assumptions to 4-7 short
bullets describing what was billed vs held and the retainage/stored-material stance.

COST CODES:
{json.dumps(compact, indent=2)}
"""


def generate_aia_for_job(
    period: str,
    job_code: str,
    *,
    application_no: str | None = None,
    retainage_pct: float = 0.05,
    previous_completed_to_date: float = 0.0,
    prepared_date: str | None = None,
) -> dict:
    """Build a draft AIA application for one job + month. Returns a structured dict.

    Dollar math is computed here; Gemini only classifies billed/held + notes.
    `previous_completed_to_date` is the cumulative completed value from prior
    applications (0 for the first application).
    """
    from api.database import SessionLocal
    from api.db_services import get_billing_draft
    from billing_draft_tracking.gemini_service import generate_json, model_info

    db = SessionLocal()
    try:
        rows = get_billing_draft(db, period, job_code)
    finally:
        db.close()
    if not rows:
        raise ValueError(f"No billing draft rows for period={period} job_code={job_code}. Build it first.")

    job_description = rows[0]["job_description"] or ""
    contract_sum = round(sum(float(r["budgeted_all_cost"] or 0) for r in rows), 2)

    # ── Gemini: per-line bill/hold decisions + notes ──────────────────────
    llm = generate_json(SYSTEM_INSTRUCTION, _build_prompt(job_code, job_description, period, rows))
    decisions = {d.get("cost_code"): d for d in llm.get("line_items", [])}
    assumptions = llm.get("assumptions", [])

    # ── Python: deterministic G702 math from the decisions ────────────────
    line_items: list[dict] = []
    gross_completed = 0.0
    items_billed = 0
    items_held = 0
    for r in rows:
        d = decisions.get(r["cost_code"], {})
        earned = round(float(r["capped_expected_all_cost"] or 0), 2)
        billed = bool(d.get("billed")) and earned > 0
        completed_this_period = earned if billed else 0.0
        if billed:
            items_billed += 1
            gross_completed += completed_this_period
        else:
            items_held += 1
        line_items.append({
            "cost_code": r["cost_code"],
            "description": r["description"],
            "unit": r["unit"],
            "scheduled_value": round(float(r["budgeted_all_cost"] or 0), 2),
            "budgeted_quantity": r["budgeted_quantity"],
            "actual_quantity": r["actual_quantity"],
            "percent_complete": r["percent_complete"],
            "earned_value_capped": earned,
            "completed_this_period": completed_this_period,
            "billed": billed,
            "hold_reason": (None if billed else (d.get("hold_reason") or "Held for manual review")),
        })

    gross_completed = round(gross_completed, 2)
    total_completed_to_date = round(previous_completed_to_date + gross_completed, 2)
    total_retainage = round(retainage_pct * total_completed_to_date, 2)
    earned_less_retainage = round(total_completed_to_date - total_retainage, 2)
    less_previous_certificates = round(previous_completed_to_date * (1 - retainage_pct), 2)
    current_payment_due = round(earned_less_retainage - less_previous_certificates, 2)
    remaining_balance = round(contract_sum - total_completed_to_date, 2)

    yr, mo = (int(x) for x in period.split("-"))
    import calendar
    period_to = f"{mo:02d}/{calendar.monthrange(yr, mo)[1]:02d}/{yr}"

    return {
        "project": {"job_code": job_code, "job_description": job_description},
        "application_no": application_no or "001 (draft)",
        "period": period,
        "period_to": period_to,
        "prepared_date": prepared_date or date.today().isoformat(),
        "summary": {
            "original_contract_sum": contract_sum,
            "net_change_by_co": 0.0,
            "contract_sum_to_date": contract_sum,
            "total_completed_stored_to_date": total_completed_to_date,
            "retainage_pct": round(retainage_pct * 100, 2),
            "total_retainage": total_retainage,
            "total_earned_less_retainage": earned_less_retainage,
            "less_previous_certificates": less_previous_certificates,
            "current_payment_due": current_payment_due,
            "gross_completed_this_period": gross_completed,
            "remaining_contract_balance": remaining_balance,
            "items_billed": items_billed,
            "items_held": items_held,
        },
        "assumptions": assumptions,
        "line_items": line_items,
        "_meta": {"model": model_info(), "cost_codes": len(rows)},
    }
