"""
projection_tracking/analyzer.py

Core Projection / Production Analysis logic.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Literal

logger = logging.getLogger(__name__)

ProjectionStatus = Literal["ON_TRACK", "AT_RISK", "OVER_BUDGET"]

DISCREPANCY_THRESHOLD_PCT = 5.0


@dataclass(frozen=True)
class ProjectionResult:
    period_start: str
    period_end: str

    job_id: str
    job_code: str
    job_name: str
    business_unit: str

    cost_code_id: str
    cost_code: str
    cost_code_description: str
    unit: str

    budgeted_quantity: float
    quantity: float
    completion_pct: float

    budgeted_cost: float
    expected: float
    actual: float
    variance: float

    projected_final: float
    projected_over_under: float
    performance_factor: float

    actual_labor_cost: float
    actual_equipment_cost: float
    actual_material_cost: float
    actual_subcontract_cost: float
    actual_trucking_cost: float

    quantity_from_job_costs: float
    labor_hours: float
    equipment_hours: float

    status: ProjectionStatus
    alert: bool
    discrepancy_flag: bool


def _safe_float(val: Any, default: float = 0.0) -> float:
    try:
        return float(val or 0)
    except (TypeError, ValueError):
        return default


def _determine_status(
    variance: float,
    projected_over_under: float,
    completion_pct: float,
) -> ProjectionStatus:
    """
    Positive variance means expected is higher than actual.
    Negative variance means actual is higher than expected.
    """
    if completion_pct <= 0:
        return "ON_TRACK"

    if variance < 0:
        return "OVER_BUDGET"

    if projected_over_under < 0:
        return "AT_RISK"

    return "ON_TRACK"


def calculate_projections(raw_items: list[dict[str, Any]]) -> list[ProjectionResult]:
    """
    Convert raw HCSS projection rows into ProjectionResult objects.

    raw_items are already calculated by ProjectionHCSSClient using the verified
    HCSS Production Analysis logic.
    """
    results: list[ProjectionResult] = []

    for item in raw_items:
        try:
            budgeted_cost = _safe_float(item.get("budgeted_cost"))
            expected = _safe_float(item.get("expected"))
            actual = _safe_float(item.get("actual"))
            variance = _safe_float(item.get("variance"))
            completion_pct = _safe_float(item.get("completion_pct"))
            projected_over_under = _safe_float(item.get("projected_over_under"))

            status = _determine_status(
                variance=variance,
                projected_over_under=projected_over_under,
                completion_pct=completion_pct,
            )

            discrepancy_flag = False
            if budgeted_cost > 0:
                projected_final = _safe_float(item.get("projected_final"))
                if projected_final > 0:
                    divergence_pct = abs((projected_final - budgeted_cost) / budgeted_cost) * 100
                    discrepancy_flag = divergence_pct > DISCREPANCY_THRESHOLD_PCT

            alert = status != "ON_TRACK" or discrepancy_flag

            results.append(
                ProjectionResult(
                    period_start=item.get("period_start", ""),
                    period_end=item.get("period_end", ""),

                    job_id=item.get("job_id", ""),
                    job_code=item.get("job_code", ""),
                    job_name=item.get("job_name", ""),
                    business_unit=item.get("business_unit", "N/A"),

                    cost_code_id=item.get("cost_code_id", ""),
                    cost_code=item.get("cost_code", ""),
                    cost_code_description=item.get("cost_code_description", ""),
                    unit=item.get("unit", ""),

                    budgeted_quantity=round(_safe_float(item.get("budgeted_quantity")), 3),
                    quantity=round(_safe_float(item.get("quantity")), 3),
                    completion_pct=round(completion_pct, 2),

                    budgeted_cost=round(budgeted_cost, 2),
                    expected=round(expected, 2),
                    actual=round(actual, 2),
                    variance=round(variance, 2),

                    projected_final=round(_safe_float(item.get("projected_final")), 2),
                    projected_over_under=round(projected_over_under, 2),
                    performance_factor=round(_safe_float(item.get("performance_factor")), 3),

                    actual_labor_cost=round(_safe_float(item.get("actual_labor_cost")), 2),
                    actual_equipment_cost=round(_safe_float(item.get("actual_equipment_cost")), 2),
                    actual_material_cost=round(_safe_float(item.get("actual_material_cost")), 2),
                    actual_subcontract_cost=round(_safe_float(item.get("actual_subcontract_cost")), 2),
                    actual_trucking_cost=round(_safe_float(item.get("actual_trucking_cost")), 2),

                    quantity_from_job_costs=round(_safe_float(item.get("quantity_from_job_costs")), 3),
                    labor_hours=round(_safe_float(item.get("labor_hours")), 2),
                    equipment_hours=round(_safe_float(item.get("equipment_hours")), 2),

                    status=status,
                    alert=alert,
                    discrepancy_flag=discrepancy_flag,
                )
            )

        except (KeyError, ValueError, TypeError) as exc:
            logger.warning(
                "Skipping invalid projection item job_id=%s cost_code_id=%s error=%s",
                item.get("job_id", "unknown"),
                item.get("cost_code_id", "unknown"),
                exc,
            )
            continue

    on_track = sum(1 for r in results if r.status == "ON_TRACK")
    at_risk = sum(1 for r in results if r.status == "AT_RISK")
    over_budget = sum(1 for r in results if r.status == "OVER_BUDGET")
    alerts = sum(1 for r in results if r.alert)

    logger.info(
        "Projection analysis complete: total=%d on_track=%d at_risk=%d over_budget=%d alerts=%d",
        len(results),
        on_track,
        at_risk,
        over_budget,
        alerts,
    )

    return results
