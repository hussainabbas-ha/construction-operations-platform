"""
Reporting and formatting utilities for budget tracking API responses.
"""
from __future__ import annotations

import logging
from typing import Any

from budget_tracking.analyzer import BudgetResult

logger = logging.getLogger(__name__)


def format_budget_items(results: list[BudgetResult]) -> list[dict[str, Any]]:
    """
    Format BudgetResult objects for JSON API response.
    
    Args:
        results: List of BudgetResult objects
        
    Returns:
        List of dicts with formatted currency and percentages
    """
    formatted_items: list[dict[str, Any]] = []
    
    for result in results:
        item = {
            "cost_code_id": result.cost_code_id,
            "cost_code": result.cost_code,
            "cost_code_description": result.cost_code_description,
            "job_id": result.job_id,
            "job_name": result.job_name,
            "business_unit": result.business_unit,
            "expected_budget": result.expected_budget,  # Float without currency symbols
            "actual_cost": result.actual_cost,  # Float without currency symbols
            "utilization_percentage": result.utilization_percentage,  # Integer
            "variance": result.variance,  # Signed float
            "status": result.status,
        }
        formatted_items.append(item)
    
    return formatted_items


def calculate_summary_counts(results: list[BudgetResult]) -> dict[str, int]:
    """
    Calculate summary statistics.
    
    Args:
        results: List of BudgetResult objects
        
    Returns:
        Dictionary with keys: total, on_track, over_risk
    """
    total = len(results)
    on_track = sum(1 for r in results if r.status == "ON_TRACK")
    over_risk = sum(1 for r in results if r.status == "OVER_RISK")
    
    logger.debug(
        "Summary counts: total=%d on_track=%d over_risk=%d",
        total,
        on_track,
        over_risk,
    )
    
    return {
        "total": total,
        "on_track": on_track,
        "over_risk": over_risk,
    }
