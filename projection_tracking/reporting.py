"""
projection_tracking/reporting.py

Reporting and formatting utilities for projection tracking API responses.
"""

from __future__ import annotations

import logging
from typing import Any

from projection_tracking.analyzer import ProjectionResult

logger = logging.getLogger(__name__)


def calculate_summary_counts(results: list[ProjectionResult]) -> dict[str, int]:
    total = len(results)
    on_track = sum(1 for r in results if r.status == "ON_TRACK")
    at_risk = sum(1 for r in results if r.status == "AT_RISK")
    over_budget = sum(1 for r in results if r.status == "OVER_BUDGET")
    alerts = sum(1 for r in results if r.alert)

    return {
        "total": total,
        "on_track": on_track,
        "at_risk": at_risk,
        "over_budget": over_budget,
        "alerts": alerts,
    }


def format_projection_items(results: list[ProjectionResult]) -> list[dict[str, Any]]:
    formatted: list[dict[str, Any]] = []

    for r in results:
        formatted.append(
            {
                "period_start": r.period_start,
                "period_end": r.period_end,

                "job_id": r.job_id,
                "job_code": r.job_code,
                "job_name": r.job_name,
                "business_unit": r.business_unit,

                "cost_code_id": r.cost_code_id,
                "cost_code": r.cost_code,
                "cost_code_description": r.cost_code_description,
                "unit": r.unit,

                "budgeted_quantity": r.budgeted_quantity,
                "quantity": r.quantity,
                "completion_pct": r.completion_pct,

                "budgeted_cost": r.budgeted_cost,
                "expected": r.expected,
                "actual": r.actual,
                "variance": r.variance,

                "projected_final": r.projected_final,
                "projected_over_under": r.projected_over_under,
                "performance_factor": r.performance_factor,

                "actual_labor_cost": r.actual_labor_cost,
                "actual_equipment_cost": r.actual_equipment_cost,
                "actual_material_cost": r.actual_material_cost,
                "actual_subcontract_cost": r.actual_subcontract_cost,
                "actual_trucking_cost": r.actual_trucking_cost,

                "quantity_from_job_costs": r.quantity_from_job_costs,
                "labor_hours": r.labor_hours,
                "equipment_hours": r.equipment_hours,

                "status": r.status,
                "alert": r.alert,
                "discrepancy_flag": r.discrepancy_flag,
            }
        )

    return formatted