"""
postmortem/analyzer.py
───────────────────────
Core aggregation engine for the Postmortem Analysis module.

DATA SOURCE CHANGE (Phase 10.1):
  Cost summary now reads from postmortem_cost_code_results and
  postmortem_job_snapshots, populated by the postmortem cron job using the
  verified Expected All Cost formula from get_job_expected_all_cost.py.

  Previous behaviour (reading from budget_tracking_results) was incorrect
  because it summed daily snapshots, over-counting actual cost, and used
  raw budgetedAllCost instead of the formula-computed expectedAllCost.

Other data sources (unchanged):
  quantity_tracking_results  → quantity completion per cost code
  projection_tracking_results → job-level EAC, billing position, status
  timelog_timecard_results   → time log quality (rejected / flagged)

Deferred (pending module completion):
  - change_orders       → Billing Draft module
  - schedule_perf       → Schedule Monitoring module
"""
from __future__ import annotations

import json
import logging
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from api.database import (
    PostmortemJobSnapshot,
    PostmortemCostCodeResult,
    QuantityTrackingResult,
    ProjectionTrackingResult,
    TimelogTimecardResult,
)
from api.db_services import (
    get_postmortem_job_snapshot,
    get_postmortem_cost_codes,
    get_postmortem_all_jobs,
)

logger = logging.getLogger(__name__)


# ── Constants ─────────────────────────────────────────────────────────────────

# A cost code is flagged as a "missed cost" candidate when:
#   - quantity is >= this threshold (work is substantially done)
#   - but cost utilisation is < this threshold (cost not captured)
MISSED_COST_QTY_THRESHOLD  = 75.0    # percent complete
MISSED_COST_UTIL_THRESHOLD = 50.0    # utilisation %

# Overall project risk thresholds (based on cost utilisation)
RISK_THRESHOLD_AT_RISK     = 75.0    # utilisation % → AT_RISK
RISK_THRESHOLD_OVER_BUDGET = 100.0   # utilisation % → OVER_BUDGET


# ── Sub-result dataclasses ────────────────────────────────────────────────────

@dataclass(frozen=True)
class CostCodeSummary:
    """Aggregated cost vs expected for a single cost code within a project."""
    cost_code_id:          str
    cost_code:             str
    cost_code_description: str
    expected_budget:       float          # Expected All Cost (formula-computed)
    actual_cost:           float
    variance:              float          # expected - actual  (positive = under budget)
    utilization_pct:       float | None
    loss_amount:           float          # max(0, actual - expected)
    loss_pct:              float | None
    status:                str            # ON_TRACK | OVER_RISK


@dataclass(frozen=True)
class QuantityCodeSummary:
    """Aggregated quantity progress for a single cost code within a project."""
    cost_code_id:       str
    cost_code:          str
    description:        str
    unit:               str
    cost_type:          str
    planned_quantity:   float
    installed_quantity: float
    remaining_quantity: float
    percent_complete:   float
    status:             str              # ON_TRACK | NEAR_COMPLETION | OVER_RISK
    alert:              bool


@dataclass(frozen=True)
class MissedCostItem:
    """
    A cost code where quantity work appears complete but cost is not captured.
    Signals a potential missed or unrecorded cost.
    """
    cost_code_id:           str
    cost_code:              str
    description:            str
    qty_percent_complete:   float
    cost_utilization_pct:   float | None
    expected_budget:        float
    actual_cost:            float
    estimated_missed_value: float        # expected_budget - actual_cost
    source:                 str          # "qty_vs_cost" | "rejected_timecards"
    detail:                 str          # human-readable explanation


@dataclass(frozen=True)
class TimelogSummary:
    """Time log quality summary for a project."""
    total_timecards:   int
    approved:          int
    flagged:           int
    rejected:          int
    rejection_reasons: list[str]         # deduplicated, most common first
    flag_reasons:      list[str]
    quality_score_pct: float             # approved / total * 100


@dataclass
class ProjectionSummary:
    """
    Latest monthly projection snapshot for a project.
    Sourced from projection_tracking_results (most recent tracking_month).
    """
    tracking_month:             str
    original_budget:            float
    actual_cost_to_date:        float
    projected_final_cost:       float
    cost_variance:              float    # original_budget - projected_final_cost
    percent_complete:           float
    original_contract_value:    float
    approved_change_orders:     float
    revised_contract_value:     float
    billed_to_date:             float
    projected_final_billing:    float
    billing_variance:           float    # billed_to_date - actual_cost_to_date
    estimated_completion_month: str
    status:                     str      # ON_TRACK | AT_RISK | OVER_BUDGET
    alert:                      bool
    discrepancy_flag:           bool


@dataclass
class PostmortemResult:
    """
    Complete postmortem analysis result for a single project.

    Cost data now sourced from postmortem_cost_code_results (Expected All Cost
    formula, verified against HCSS portal).  All other sections are unchanged.
    """
    # ── Project identity ──────────────────────────────────────────────
    job_id:        str
    job_code:      str
    job_name:      str
    business_unit: str

    # ── Overall KPIs ──────────────────────────────────────────────────
    overall_risk:          str
    total_expected_budget: float
    total_actual_cost:     float
    total_variance:        float
    total_variance_pct:    float | None
    total_loss_amount:     float
    total_loss_pct:        float | None

    # ── Section data ──────────────────────────────────────────────────
    cost_codes:   list[CostCodeSummary]
    quantities:   list[QuantityCodeSummary]
    missed_costs: list[MissedCostItem]
    timelog:      TimelogSummary | None
    projection:   ProjectionSummary | None

    # ── Deferred sections ─────────────────────────────────────────────
    change_orders:        None = field(default=None)   # Billing Draft module
    schedule_performance: None = field(default=None)   # Schedule Monitoring module

    # ── Derived counts ─────────────────────────────────────────────────
    over_risk_cost_codes:        int   = field(default=0)
    near_completion_qty_codes:   int   = field(default=0)
    over_risk_qty_codes:         int   = field(default=0)
    missed_cost_count:           int   = field(default=0)
    estimated_missed_cost_total: float = field(default=0.0)


# ── Private helpers ───────────────────────────────────────────────────────────

def _safe_float(val: Any, default: float = 0.0) -> float:
    try:
        return float(val or 0)
    except (TypeError, ValueError):
        return default


def _utilization_pct(actual: float, expected: float) -> float | None:
    if expected <= 0:
        return None
    return round((actual / expected) * 100, 2)


def _cost_status(utilization: float | None, actual: float) -> str:
    if utilization is None:
        return "OVER_RISK" if actual > 0 else "ON_TRACK"
    return "OVER_RISK" if utilization >= RISK_THRESHOLD_AT_RISK else "ON_TRACK"


def _overall_risk(utilization: float | None, actual: float) -> str:
    if utilization is None:
        return "OVER_BUDGET" if actual > 0 else "ON_TRACK"
    if utilization >= RISK_THRESHOLD_OVER_BUDGET:
        return "OVER_BUDGET"
    if utilization >= RISK_THRESHOLD_AT_RISK:
        return "AT_RISK"
    return "ON_TRACK"


def _parse_json_list(raw: str) -> list[str]:
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, list) else []
    except (json.JSONDecodeError, TypeError):
        return []


# ── Section builders ──────────────────────────────────────────────────────────

def _build_cost_summary(
    db: Session,
    job_id: str,
) -> list[CostCodeSummary]:
    """
    Build per-cost-code cost summaries from postmortem_cost_code_results.

    This is a direct read — one row per cost code, no aggregation needed
    because the postmortem cron job already stored the correct values using
    the verified Expected All Cost formula.
    """
    rows: list[PostmortemCostCodeResult] = get_postmortem_cost_codes(db, job_id)

    if not rows:
        logger.debug("No postmortem cost code rows for job_id=%s", job_id)
        return []

    results: list[CostCodeSummary] = []
    for row in rows:
        expected = _safe_float(row.expected_all_cost)
        actual   = _safe_float(row.actual_cost)
        util     = _utilization_pct(actual, expected)
        variance = round(expected - actual, 2)
        loss     = round(max(0.0, actual - expected), 2)
        loss_pct = round((loss / expected) * 100, 2) if expected > 0 else None

        results.append(CostCodeSummary(
            cost_code_id          = row.cost_code_id,
            cost_code             = row.cost_code or "",
            cost_code_description = row.description or "",
            expected_budget       = round(expected, 2),
            actual_cost           = round(actual, 2),
            variance              = variance,
            utilization_pct       = util,
            loss_amount           = loss,
            loss_pct              = loss_pct,
            status                = _cost_status(util, actual),
        ))

    logger.debug(
        "Cost summary for job_id=%s: %d cost codes (from postmortem_cost_code_results)",
        job_id, len(results),
    )
    return results


def _build_quantity_summary(
    db: Session,
    job_id: str,
) -> list[QuantityCodeSummary]:
    """
    Aggregate quantity_tracking_results for a job across daily keys only.

    Excludes legacy start__end period snapshot keys to avoid double-counting.
    Takes planned_quantity from the most recent daily row (static budget value).
    Sums installed_quantity across all daily dates.
    Includes all cost codes (no planned/installed filter).
    remaining_quantity = planned − installed (can be negative).
    """
    rows: list[QuantityTrackingResult] = (
        db.query(QuantityTrackingResult)
        .filter(QuantityTrackingResult.job_id == job_id)
        .order_by(QuantityTrackingResult.tracking_date.asc())
        .all()
    )

    if not rows:
        logger.debug("No quantity rows for job_id=%s", job_id)
        return []

    agg: dict[str, dict[str, Any]] = {}
    for row in rows:
        # Daily keys only — never sum period snapshots (contain "__")
        td = row.tracking_date or ""
        if len(td) != 10 or "__" in td:
            continue
        ccid = row.cost_code_id
        if ccid in agg:
            agg[ccid]["installed_quantity"] += _safe_float(row.installed_quantity)
            agg[ccid]["planned_quantity"]    = _safe_float(row.planned_quantity)
        else:
            agg[ccid] = {
                "cost_code_id":      ccid,
                "cost_code":         row.cost_code,
                "description":       row.description,
                "unit":              row.unit,
                "cost_type":         row.cost_type,
                "planned_quantity":  _safe_float(row.planned_quantity),
                "installed_quantity": _safe_float(row.installed_quantity),
            }

    results: list[QuantityCodeSummary] = []
    for d in agg.values():
        planned   = d["planned_quantity"]
        installed = d["installed_quantity"]
        # Show all cost codes — no planned/installed write-style filter.
        if planned > 0:
            pct = round((installed / planned) * 100, 2)
        else:
            pct = 0.0
        remaining = round(planned - installed, 4)
        status    = (
            "OVER_RISK"        if pct >= 100.0
            else "NEAR_COMPLETION" if pct >= 75.0
            else "ON_TRACK"
        )

        results.append(QuantityCodeSummary(
            cost_code_id      = d["cost_code_id"],
            cost_code         = d["cost_code"],
            description       = d["description"],
            unit              = d["unit"],
            cost_type         = d["cost_type"],
            planned_quantity  = round(planned, 4),
            installed_quantity = round(installed, 4),
            remaining_quantity = remaining,
            percent_complete  = pct,
            status            = status,
            alert             = pct >= 75.0,
        ))

    logger.debug(
        "Quantity summary for job_id=%s: %d cost codes from %d DB rows",
        job_id, len(results), len(rows),
    )
    return results


def _build_missed_costs(
    cost_codes: list[CostCodeSummary],
    quantities: list[QuantityCodeSummary],
    timelog:    "TimelogSummary | None",
) -> list[MissedCostItem]:
    """
    Detect potential missed / unrecorded costs by cross-referencing
    quantity completion against cost utilisation.

    Detection rule:
        quantity percent_complete >= MISSED_COST_QTY_THRESHOLD  (work is done)
        AND cost utilisation_pct  <  MISSED_COST_UTIL_THRESHOLD  (cost missing)
    """
    cost_map: dict[str, CostCodeSummary] = {c.cost_code_id: c for c in cost_codes}
    missed: list[MissedCostItem] = []

    for qty in quantities:
        if qty.percent_complete < MISSED_COST_QTY_THRESHOLD:
            continue

        cost = cost_map.get(qty.cost_code_id)
        if cost is None:
            continue

        util = cost.utilization_pct
        if util is not None and util >= MISSED_COST_UTIL_THRESHOLD:
            continue

        estimated_missed = round(max(0.0, cost.expected_budget - cost.actual_cost), 2)
        if estimated_missed <= 0:
            continue

        detail = (
            f"Quantity {qty.percent_complete:.1f}% complete "
            f"({qty.installed_quantity:,.2f} / {qty.planned_quantity:,.2f} {qty.unit}) "
            f"but cost utilisation is only "
            f"{f'{util:.1f}%' if util is not None else 'N/A'} "
            f"(${cost.actual_cost:,.2f} of ${cost.expected_budget:,.2f} expected). "
            f"Estimated unrecorded cost: ${estimated_missed:,.2f}."
        )

        missed.append(MissedCostItem(
            cost_code_id           = qty.cost_code_id,
            cost_code              = qty.cost_code,
            description            = qty.description,
            qty_percent_complete   = qty.percent_complete,
            cost_utilization_pct   = util,
            expected_budget        = cost.expected_budget,
            actual_cost            = cost.actual_cost,
            estimated_missed_value = estimated_missed,
            source                 = "qty_vs_cost",
            detail                 = detail,
        ))

    # Second signal: rejected timecards → potential unrecorded labour
    if timelog and timelog.rejected > 0:
        detail = (
            f"{timelog.rejected} timecard(s) were rejected for this project. "
            "Rejected timecards may contain unrecorded labour hours and quantities. "
            f"Common reasons: {'; '.join(timelog.rejection_reasons[:3]) or 'see timelog tab'}."
        )
        missed.append(MissedCostItem(
            cost_code_id           = "TIMELOG",
            cost_code              = "TIMELOG",
            description            = "Rejected Timecards — Potential Unrecorded Labour",
            qty_percent_complete   = 0.0,
            cost_utilization_pct   = None,
            expected_budget        = 0.0,
            actual_cost            = 0.0,
            estimated_missed_value = 0.0,
            source                 = "rejected_timecards",
            detail                 = detail,
        ))

    logger.debug("Missed cost detection: %d item(s) flagged", len(missed))
    return missed


def _build_timelog_summary(
    db: Session,
    job_id: str,
) -> TimelogSummary | None:
    """Aggregate timelog_timecard_results for a job. Returns None if no data."""
    rows: list[TimelogTimecardResult] = (
        db.query(TimelogTimecardResult)
        .filter(TimelogTimecardResult.job_id == job_id)
        .all()
    )

    if not rows:
        logger.debug("No timelog rows for job_id=%s", job_id)
        return None

    total    = len(rows)
    approved = sum(1 for r in rows if r.status == "APPROVED")
    flagged  = sum(1 for r in rows if r.status == "FLAGGED")
    rejected = sum(1 for r in rows if r.status == "REJECTED")

    all_reasons: list[str] = []
    all_flags:   list[str] = []
    for row in rows:
        all_reasons.extend(_parse_json_list(row.reasons))
        all_flags.extend(_parse_json_list(row.flags))

    reason_counts = Counter(all_reasons)
    flag_counts   = Counter(all_flags)
    unique_reasons = [r for r, _ in reason_counts.most_common()]
    unique_flags   = [f for f, _ in flag_counts.most_common()]

    quality_score = round((approved / total) * 100, 2) if total > 0 else 0.0

    logger.debug(
        "Timelog for job_id=%s: total=%d approved=%d flagged=%d rejected=%d",
        job_id, total, approved, flagged, rejected,
    )
    return TimelogSummary(
        total_timecards   = total,
        approved          = approved,
        flagged           = flagged,
        rejected          = rejected,
        rejection_reasons = unique_reasons,
        flag_reasons      = unique_flags,
        quality_score_pct = quality_score,
    )


def _build_projection_summary(
    db: Session,
    job_id: str,
    snapshot: "PostmortemJobSnapshot",
) -> ProjectionSummary | None:
    """
    Build the projection summary for a job using verified postmortem snapshot values.

    All cost-derived fields are computed from the postmortem snapshot so they
    are fully consistent with the top-level KPI numbers in the same response.

    Formulas
    --------
    expected_all_cost  = snapshot.expected_all_cost   (verified HCSS formula)
    actual_cost        = snapshot.actual_cost         (authoritative HCSS totalCost)

    percent_complete   = (actual / expected) * 100
        How much of the expected budget has been consumed.
        Proxy for cost-based % complete — the same basis HCSS uses.

    projected_final_cost = expected / (expected / budgeted) when budgeted > 0
        Standard EAC (Estimate at Completion) = actual / CPI
        where CPI (Cost Performance Index) = expected / actual.
        Simplifies to: projected_final = budgeted_all_cost * (actual / expected)
        i.e. scale the original budget by your current cost-efficiency ratio.
        When expected == 0 → projected_final = actual (nothing budgeted/earned).

    cost_variance      = expected - actual
        Positive = under budget (you've spent less than expected).
        Negative = over budget.

    status / alert / discrepancy_flag are recomputed to stay consistent with
    the new numbers (the DB row values were based on stale/wrong data).

    Billing fields (original_contract_value, approved_change_orders,
    revised_contract_value, billed_to_date, projected_final_billing,
    billing_variance) are read from the DB row — they come from the billing
    module and are 0 until that module is connected.
    """
    row: ProjectionTrackingResult | None = (
        db.query(ProjectionTrackingResult)
        .filter(ProjectionTrackingResult.job_id == job_id)
        .order_by(ProjectionTrackingResult.tracking_month.desc())
        .first()
    )

    if row is None:
        logger.debug("No projection row for job_id=%s", job_id)
        return None

    # ── Core values from snapshot (authoritative) ──────────────────────────
    expected = round(_safe_float(snapshot.expected_all_cost), 2)
    actual   = round(_safe_float(snapshot.actual_cost), 2)
    budgeted = round(_safe_float(snapshot.budgeted_all_cost), 2)

    # ── Derived calculations ───────────────────────────────────────────────

    # % complete: cost utilisation = how much of expected budget is consumed
    if expected > 0:
        percent_complete = round((actual / expected) * 100, 2)
    else:
        percent_complete = 0.0

    # EAC (Estimate at Completion) using CPI method:
    #   CPI  = expected / actual  (earned value / actual cost)
    #   EAC  = budgeted_all_cost / CPI  =  budgeted * (actual / expected)
    # When expected == 0: no earned value, set EAC = actual (spend so far)
    if expected > 0 and budgeted > 0:
        projected_final_cost = round(budgeted * (actual / expected), 2)
    elif actual > 0:
        projected_final_cost = actual
    else:
        projected_final_cost = 0.0

    # Cost variance: expected − actual (positive = under budget)
    cost_variance = round(expected - actual, 2)

    # ── Status recomputed from fresh numbers ───────────────────────────────
    if percent_complete <= 0:
        proj_status = "ON_TRACK"
    elif cost_variance < 0:
        # Actual exceeds expected this period → over budget
        proj_status = "OVER_BUDGET"
    elif budgeted > 0 and projected_final_cost > budgeted:
        # EAC exceeds original budget → at risk
        proj_status = "AT_RISK"
    else:
        proj_status = "ON_TRACK"

    # Discrepancy flag: EAC diverges > 5% from budgeted
    discrepancy_flag = False
    if budgeted > 0 and projected_final_cost > 0:
        divergence_pct = abs((projected_final_cost - budgeted) / budgeted) * 100
        discrepancy_flag = divergence_pct > 5.0

    alert = proj_status != "ON_TRACK" or discrepancy_flag

    return ProjectionSummary(
        tracking_month             = row.tracking_month,
        original_budget            = expected,           # Expected All Cost = our budget basis
        actual_cost_to_date        = actual,             # Authoritative HCSS totalCost
        projected_final_cost       = projected_final_cost,
        cost_variance              = cost_variance,
        percent_complete           = percent_complete,
        # Billing fields — from DB row (0.0 until billing module is live)
        original_contract_value    = _safe_float(row.original_contract_value),
        approved_change_orders     = _safe_float(row.approved_change_orders),
        revised_contract_value     = _safe_float(row.revised_contract_value),
        billed_to_date             = _safe_float(row.billed_to_date),
        projected_final_billing    = _safe_float(row.projected_final_billing),
        billing_variance           = _safe_float(row.billing_variance),
        estimated_completion_month = row.estimated_completion_month or "",
        status                     = proj_status,
        alert                      = alert,
        discrepancy_flag           = discrepancy_flag,
    )


# ── Public API ────────────────────────────────────────────────────────────────

def build_postmortem_for_job(
    db: Session,
    job_id: str,
) -> PostmortemResult | None:
    """
    Build a complete postmortem analysis for a single project.

    Primary data source: postmortem_job_snapshots + postmortem_cost_code_results
    (populated by the postmortem cron job with the verified Expected All Cost formula).

    Secondary sources (unchanged): quantity_tracking_results, projection_tracking_results,
    timelog_timecard_results.

    Returns None if no postmortem data exists for the given job_id.

    Args:
        db:     SQLAlchemy session (caller owns lifecycle).
        job_id: HCSS job UUID.
    """
    logger.info("Building postmortem for job_id=%s", job_id)

    # ── Resolve job identity from postmortem snapshot ──────────────────────
    snapshot: PostmortemJobSnapshot | None = get_postmortem_job_snapshot(db, job_id)

    if snapshot is None:
        logger.warning(
            "No postmortem snapshot for job_id=%s — "
            "run the postmortem cron job to populate data.",
            job_id,
        )
        return None

    job_code      = snapshot.job_code or ""
    job_name      = snapshot.job_name or ""
    business_unit = snapshot.business_unit or "N/A"

    # ── Build each section ─────────────────────────────────────────────────
    cost_codes = _build_cost_summary(db, job_id)
    quantities = _build_quantity_summary(db, job_id)
    timelog    = _build_timelog_summary(db, job_id)
    projection = _build_projection_summary(db, job_id, snapshot)
    missed     = _build_missed_costs(cost_codes, quantities, timelog)

    # ── Project-level totals ───────────────────────────────────────────────
    # Use the job snapshot's actual_cost (authoritative totalCost from HCSS)
    # rather than summing cost_codes, which may miss unbudgeted codes.
    total_expected = _safe_float(snapshot.expected_all_cost)
    total_actual   = _safe_float(snapshot.actual_cost)
    total_variance = round(total_expected - total_actual, 2)
    total_util     = _utilization_pct(total_actual, total_expected)
    total_loss     = round(max(0.0, total_actual - total_expected), 2)
    total_loss_pct = (
        round((total_loss / total_expected) * 100, 2) if total_expected > 0 else None
    )

    # ── Derived counts ─────────────────────────────────────────────────────
    over_risk_cc = sum(1 for c in cost_codes if c.status == "OVER_RISK")
    near_qty     = sum(1 for q in quantities if q.status == "NEAR_COMPLETION")
    over_qty     = sum(1 for q in quantities if q.status == "OVER_RISK")
    missed_count = len(missed)
    missed_total = round(sum(m.estimated_missed_value for m in missed), 2)

    result = PostmortemResult(
        job_id                     = job_id,
        job_code                   = job_code,
        job_name                   = job_name,
        business_unit              = business_unit,
        overall_risk               = _overall_risk(total_util, total_actual),
        total_expected_budget      = round(total_expected, 2),
        total_actual_cost          = round(total_actual, 2),
        total_variance             = total_variance,
        total_variance_pct         = total_util,
        total_loss_amount          = total_loss,
        total_loss_pct             = total_loss_pct,
        cost_codes                 = cost_codes,
        quantities                 = quantities,
        missed_costs               = missed,
        timelog                    = timelog,
        projection                 = projection,
        over_risk_cost_codes       = over_risk_cc,
        near_completion_qty_codes  = near_qty,
        over_risk_qty_codes        = over_qty,
        missed_cost_count          = missed_count,
        estimated_missed_cost_total = missed_total,
    )

    logger.info(
        "Postmortem built: job_id=%s job_code=%s risk=%s "
        "expected=$%.2f actual=$%.2f variance=$%.2f "
        "cost_codes=%d qty_codes=%d missed=%d",
        job_id, job_code, result.overall_risk,
        total_expected, total_actual, total_variance,
        len(cost_codes), len(quantities), missed_count,
    )
    return result


def get_all_job_ids(db: Session) -> list[dict[str, str]]:
    """
    Return all jobs that have postmortem snapshot data, ordered by job_code.

    Each entry: {"job_id": str, "job_code": str, "job_name": str, "business_unit": str}

    This replaces the old multi-table fallback logic — the postmortem snapshot
    table is now the single source of truth for job discovery in this module.
    """
    snapshots: list[PostmortemJobSnapshot] = get_postmortem_all_jobs(db)

    jobs = [
        {
            "job_id":        s.job_id,
            "job_code":      s.job_code or "",
            "job_name":      s.job_name or "",
            "business_unit": s.business_unit or "N/A",
        }
        for s in snapshots
    ]

    logger.info("get_all_job_ids: %d job(s) in postmortem_job_snapshots", len(jobs))
    return jobs
