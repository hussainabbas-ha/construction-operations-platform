"""
schedule_monitoring/burn_rate.py
──────────────────────────────────
Calculates burn rate trends and projected completion dates
using only actual data from the DB (no baseline required).

For each job / cost code we compute:
  - Weekly quantity burn rate  (units/week)
  - Weekly cost burn rate      ($/week)
  - Projected completion date  (when will budget / quantity run out at current pace)
  - Weeks remaining at current pace
  - Trend direction            (accelerating / decelerating / steady)

All data comes from budget_tracking_results and quantity_tracking_results
which the cron job keeps fresh — no HCSS calls needed here.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

logger = logging.getLogger(__name__)

# Minimum weeks of data needed to calculate a meaningful rate
MIN_WEEKS_FOR_TREND = 2


@dataclass
class CostCodeBurnRate:
    cost_code_id: str
    cost_code: str
    description: str
    job_id: str
    job_code: str
    unit: str

    # Quantity burn
    total_planned_quantity: float
    total_installed_quantity: float
    quantity_pct: float
    weekly_qty_rate: float              # units installed per week (avg)
    weeks_to_qty_complete: float | None # None = already done or no rate
    projected_qty_completion: str | None  # YYYY-MM-DD

    # Cost burn
    total_budget_cost: float
    total_actual_cost: float
    cost_pct: float
    weekly_cost_rate: float             # $/week avg
    weeks_to_budget_exhaust: float | None
    projected_budget_exhaust: str | None  # YYYY-MM-DD

    # Trend (last 2 weeks vs prior 2 weeks)
    qty_trend: str   # "accelerating" | "decelerating" | "steady" | "insufficient_data"
    cost_trend: str

    # Alert flags
    budget_overrun_risk: bool   # projected exhaustion before qty completion
    seasonal_risk: str | None   # "asphalt" | "concrete" | None


@dataclass
class JobBurnRate:
    job_id: str
    job_code: str
    job_name: str

    total_budget_cost: float
    total_actual_cost: float
    cost_pct: float
    weekly_cost_rate: float
    weeks_to_budget_exhaust: float | None
    projected_budget_exhaust: str | None

    total_planned_quantity: float
    total_installed_quantity: float
    quantity_pct: float
    weekly_qty_rate: float
    weeks_to_qty_complete: float | None
    projected_qty_completion: str | None

    cost_trend: str
    qty_trend: str

    cost_codes: list[CostCodeBurnRate]


def calculate_burn_rates(
    *,
    lookback_weeks: int = 8,
    db=None,
) -> list[JobBurnRate]:
    """
    Calculate burn rates for all jobs with data in the DB.

    Args:
        lookback_weeks: How many weeks of history to use (default 8).
        db: SQLAlchemy session (opens own if None).

    Returns:
        List of JobBurnRate — one per job that has data.
    """
    owns_db = False
    if db is None:
        from api.database import SessionLocal
        db = SessionLocal()
        owns_db = True

    try:
        return _calculate(lookback_weeks=lookback_weeks, db=db)
    finally:
        if owns_db:
            db.close()


def _calculate(*, lookback_weeks: int, db) -> list[JobBurnRate]:
    from api.db_services import get_quantity_tracking, get_budget_tracking

    # Build weekly buckets: week_start → {(job_id, cc_id): {qty, cost}}
    today       = date.today()
    end_date    = today
    start_date  = today - timedelta(weeks=lookback_weeks)

    # Collect daily data from DB, bucketed by ISO week
    # week_key = "YYYY-WW"
    WeekKey = str
    CcKey   = tuple[str, str]   # (job_id, cc_id)

    qty_by_week:  dict[WeekKey, dict[CcKey, float]] = {}
    cost_by_week: dict[WeekKey, dict[CcKey, float]] = {}

    # Also track cumulative totals and metadata
    qty_totals:    dict[CcKey, dict[str, Any]] = {}   # latest planned/installed
    budget_totals: dict[CcKey, dict[str, Any]] = {}   # latest budget/actual

    current = start_date
    while current <= end_date:
        d_str    = current.strftime("%Y-%m-%d")
        week_key = current.strftime("%G-%V")   # ISO year-week

        # Quantity
        _, qty_rows = get_quantity_tracking(db, d_str)
        for r in qty_rows:
            jid  = r.get("job_id", "")
            ccid = r.get("cost_code_id", "")
            if not jid or not ccid:
                continue
            key = (jid, ccid)
            installed = float(r.get("installed_quantity", 0) or 0)

            qty_by_week.setdefault(week_key, {})
            qty_by_week[week_key][key] = (
                qty_by_week[week_key].get(key, 0.0) + installed
            )

            # Keep latest metadata
            qty_totals[key] = r

        # Budget
        _, budget_rows = get_budget_tracking(db, d_str)
        for r in budget_rows:
            jid  = r.get("job_id", "")
            ccid = r.get("cost_code_id", "")
            if not jid or not ccid:
                continue
            key = (jid, ccid)
            actual = float(r.get("actual_cost", 0) or 0)

            cost_by_week.setdefault(week_key, {})
            cost_by_week[week_key][key] = (
                cost_by_week[week_key].get(key, 0.0) + actual
            )

            budget_totals[key] = r

        current += timedelta(days=1)

    if not qty_totals and not budget_totals:
        logger.warning("No DB data found for burn rate calculation")
        return []

    all_cc_keys: set[CcKey] = set(qty_totals) | set(budget_totals)
    sorted_weeks = sorted(qty_by_week.keys() | cost_by_week.keys())

    # ── Per-cost-code burn rates ────────────────────────────────────────────────
    cc_rates: dict[CcKey, CostCodeBurnRate] = {}

    for key in all_cc_keys:
        jid, ccid = key

        q_meta = qty_totals.get(key, {})
        b_meta = budget_totals.get(key, {})

        planned_qty  = float(q_meta.get("planned_quantity", 0) or 0)
        installed    = float(q_meta.get("installed_quantity", 0) or 0)
        budget_cost  = float(b_meta.get("expected_budget", 0) or 0)
        actual_cost  = float(b_meta.get("actual_cost", 0) or 0)

        qty_pct  = round(installed / planned_qty * 100, 2) if planned_qty > 0 else 0.0
        cost_pct = round(actual_cost / budget_cost * 100, 2) if budget_cost > 0 else 0.0

        # Weekly series
        qty_series  = [qty_by_week.get(wk, {}).get(key, 0.0) for wk in sorted_weeks]
        cost_series = [cost_by_week.get(wk, {}).get(key, 0.0) for wk in sorted_weeks]

        weekly_qty_rate  = _avg_rate(qty_series)
        weekly_cost_rate = _avg_rate(cost_series)

        # Projections
        remaining_qty  = planned_qty - installed
        remaining_cost = max(0.0, budget_cost - actual_cost)

        wks_qty  = (remaining_qty  / weekly_qty_rate)  if weekly_qty_rate  > 0 else None
        wks_cost = (remaining_cost / weekly_cost_rate) if weekly_cost_rate > 0 else None

        proj_qty_date  = _weeks_from_today(wks_qty)
        proj_cost_date = _weeks_from_today(wks_cost)

        qty_trend  = _trend(qty_series)
        cost_trend = _trend(cost_series)

        # Budget overrun risk: will budget run out before work is done?
        budget_overrun_risk = False
        if proj_cost_date and proj_qty_date:
            budget_overrun_risk = proj_cost_date < proj_qty_date

        # Seasonal
        text = (
            (q_meta.get("description") or b_meta.get("cost_code_description") or "")
            + " "
            + (q_meta.get("cost_code") or b_meta.get("cost_code") or "")
        ).lower()
        seasonal = _seasonal_risk(text, today)

        cc_rates[key] = CostCodeBurnRate(
            cost_code_id=ccid,
            cost_code=q_meta.get("cost_code") or b_meta.get("cost_code") or ccid[:8],
            description=(
                q_meta.get("description")
                or b_meta.get("cost_code_description")
                or "No description"
            ),
            job_id=jid,
            job_code=q_meta.get("job_code") or b_meta.get("job_name", "")[:8] or jid[:8],
            unit=q_meta.get("unit") or "UNIT",
            total_planned_quantity=planned_qty,
            total_installed_quantity=installed,
            quantity_pct=qty_pct,
            weekly_qty_rate=round(weekly_qty_rate, 4),
            weeks_to_qty_complete=round(wks_qty, 1) if wks_qty is not None else None,
            projected_qty_completion=proj_qty_date,
            total_budget_cost=budget_cost,
            total_actual_cost=actual_cost,
            cost_pct=cost_pct,
            weekly_cost_rate=round(weekly_cost_rate, 2),
            weeks_to_budget_exhaust=round(wks_cost, 1) if wks_cost is not None else None,
            projected_budget_exhaust=proj_cost_date,
            qty_trend=qty_trend,
            cost_trend=cost_trend,
            budget_overrun_risk=budget_overrun_risk,
            seasonal_risk=seasonal,
        )

    # ── Roll up to job level ──────────────────────────────────────────────────
    job_cc_map: dict[str, list[CostCodeBurnRate]] = {}
    for (jid, _), cc_rate in cc_rates.items():
        job_cc_map.setdefault(jid, []).append(cc_rate)

    # Job metadata from DB rows
    job_meta: dict[str, dict[str, str]] = {}
    for (jid, ccid), r in qty_totals.items():
        if jid not in job_meta:
            job_meta[jid] = {
                "job_code": r.get("job_code", jid[:8]),
                "job_name": r.get("job_code", jid[:8]),
            }
    for (jid, ccid), r in budget_totals.items():
        if jid not in job_meta:
            job_meta[jid] = {
                "job_code": r.get("job_id", jid[:8])[:8],
                "job_name": r.get("job_name", jid[:8]),
            }
        else:
            if not job_meta[jid].get("job_name") or job_meta[jid]["job_name"] == jid[:8]:
                job_meta[jid]["job_name"] = r.get("job_name", jid[:8])

    job_rates: list[JobBurnRate] = []
    for jid, cc_list in sorted(job_cc_map.items()):
        meta = job_meta.get(jid, {})

        total_budget   = sum(cc.total_budget_cost for cc in cc_list)
        total_actual   = sum(cc.total_actual_cost for cc in cc_list)
        total_planned  = sum(cc.total_planned_quantity for cc in cc_list)
        total_installed = sum(cc.total_installed_quantity for cc in cc_list)
        total_wqr      = sum(cc.weekly_qty_rate for cc in cc_list)
        total_wcr      = sum(cc.weekly_cost_rate for cc in cc_list)

        cost_pct = round(total_actual / total_budget * 100, 2) if total_budget > 0 else 0.0
        qty_pct  = round(total_installed / total_planned * 100, 2) if total_planned > 0 else 0.0

        rem_qty  = max(0.0, total_planned - total_installed)
        rem_cost = max(0.0, total_budget  - total_actual)

        wks_qty  = (rem_qty  / total_wqr)  if total_wqr  > 0 else None
        wks_cost = (rem_cost / total_wcr)  if total_wcr  > 0 else None

        # Dominant trend across cost codes
        cost_trends = [cc.cost_trend for cc in cc_list if cc.cost_trend != "insufficient_data"]
        qty_trends  = [cc.qty_trend  for cc in cc_list if cc.qty_trend  != "insufficient_data"]
        job_cost_trend = _dominant_trend(cost_trends)
        job_qty_trend  = _dominant_trend(qty_trends)

        job_rates.append(JobBurnRate(
            job_id=jid,
            job_code=meta.get("job_code", jid[:8]),
            job_name=meta.get("job_name", jid[:8]),
            total_budget_cost=total_budget,
            total_actual_cost=total_actual,
            cost_pct=cost_pct,
            weekly_cost_rate=round(total_wcr, 2),
            weeks_to_budget_exhaust=round(wks_cost, 1) if wks_cost is not None else None,
            projected_budget_exhaust=_weeks_from_today(wks_cost),
            total_planned_quantity=total_planned,
            total_installed_quantity=total_installed,
            quantity_pct=qty_pct,
            weekly_qty_rate=round(total_wqr, 4),
            weeks_to_qty_complete=round(wks_qty, 1) if wks_qty is not None else None,
            projected_qty_completion=_weeks_from_today(wks_qty),
            cost_trend=job_cost_trend,
            qty_trend=job_qty_trend,
            cost_codes=sorted(cc_list, key=lambda c: c.cost_code),
        ))

    logger.info(
        "Burn rate calculated: %d jobs / %d cost codes | lookback=%d weeks",
        len(job_rates),
        sum(len(j.cost_codes) for j in job_rates),
        lookback_weeks,
    )
    return job_rates


# ── Helpers ───────────────────────────────────────────────────────────────────

def _avg_rate(series: list[float]) -> float:
    """Average of non-zero weeks."""
    non_zero = [v for v in series if v > 0]
    return sum(non_zero) / len(non_zero) if non_zero else 0.0


def _weeks_from_today(weeks: float | None) -> str | None:
    if weeks is None or weeks <= 0:
        return None
    target = date.today() + timedelta(weeks=weeks)
    return target.strftime("%Y-%m-%d")


def _trend(series: list[float]) -> str:
    """Compare last 2 non-zero weeks vs prior 2 non-zero weeks."""
    non_zero = [v for v in series if v > 0]
    if len(non_zero) < MIN_WEEKS_FOR_TREND * 2:
        return "insufficient_data"
    recent = sum(non_zero[-2:]) / 2
    prior  = sum(non_zero[-4:-2]) / 2
    if prior == 0:
        return "insufficient_data"
    delta_pct = (recent - prior) / prior * 100
    if delta_pct > 10:
        return "accelerating"
    if delta_pct < -10:
        return "decelerating"
    return "steady"


def _dominant_trend(trends: list[str]) -> str:
    if not trends:
        return "insufficient_data"
    from collections import Counter
    return Counter(trends).most_common(1)[0][0]


def _seasonal_risk(text: str, today: date) -> str | None:
    asphalt_deadline  = date(today.year, 9, 30)
    concrete_deadline = date(today.year, 10, 15)
    if ("asphalt" in text or "paving" in text or "pave" in text) and today > asphalt_deadline:
        return "asphalt"
    if "concrete" in text and today > concrete_deadline:
        return "concrete"
    return None
