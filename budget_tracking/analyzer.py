"""
Budget analysis logic — parses HCSS raw items into BudgetResult objects.

Terminology
-----------
budgeted_all_cost
    laborDollars + equipmentDollars + materialDollars + subcontractDollars
    + supplyDollars + customCostTypeDollars  (full HCSS planned budget).
    This is the ONLY value stored in the DB column `budgeted_all_cost`.

expected_budget  (quantity-weighted expected cost)
    = (installed_quantity / planned_quantity) * budgeted_all_cost
    Both installed_quantity and planned_quantity come from the quantity tracking
    DB — the same values returned by the quantity/verify API.
    Computed on the fly at response time in budget.py once quantity data is joined.
    Zero when no quantity data is available.

All downstream metrics — utilization_percentage, variance, loss_amount,
loss_pct, status — are computed at response time against expected_budget.
BudgetResult stores NO pre-computed metrics; they are all derived later.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Literal

logger = logging.getLogger(__name__)

Status = Literal["ON_TRACK", "OVER_RISK"]


@dataclass(frozen=True)
class BudgetResult:
    """
    Parsed budget item straight from the HCSS API response.

    Only budgeted_all_cost and raw cost/qty fields are populated here.
    All metrics (utilization, variance, status …) are computed later at
    response time once quantity data is available.
    """
    cost_code_id: str
    cost_code: str
    cost_code_description: str
    job_id: str
    job_name: str
    business_unit: str
    budgeted_all_cost: float   # stored in DB — used to compute expected_budget
    actual_cost: float


def calculate_budget_status(raw_items: list[dict[str, Any]]) -> list[BudgetResult]:
    """
    Parse HCSS raw items into BudgetResult objects.

    No metrics are computed here — they are all derived at response time
    using expected_budget = (actual_qty / planned_qty) * budgeted_all_cost.
    """
    results: list[BudgetResult] = []

    for item in raw_items:
        try:
            job_info = item.get("job", {})
            job_id   = job_info.get("jobId", "")
            job_name = (
                job_info.get("jobCode", "") + " - " + job_info.get("jobDescription", "")
            )

            cost_code_info        = item.get("costCode", {})
            cost_code_id          = cost_code_info.get("costCodeId", "")
            cost_code             = cost_code_info.get("costCodeCode", "")
            cost_code_description = cost_code_info.get("costCodeDescription", "")

            business_unit     = item.get("businessUnit", "N/A")
            # "expectedBudget" in the HCSS payload = our budgeted_all_cost
            budgeted_all_cost = float(item.get("expectedBudget", 0))
            actual_cost       = float(item.get("actualCost", 0))

            results.append(BudgetResult(
                cost_code_id=cost_code_id,
                cost_code=cost_code,
                cost_code_description=cost_code_description,
                job_id=job_id,
                job_name=job_name,
                business_unit=business_unit,
                budgeted_all_cost=budgeted_all_cost,
                actual_cost=actual_cost,
            ))

        except (KeyError, ValueError, TypeError) as exc:
            logger.warning(
                "Skipping invalid budget item: %s (error: %s)",
                item.get("costCode", {}).get("costCodeId", "unknown"),
                exc,
            )
            continue

    logger.info("Processed %d budget items", len(results))
    return results
