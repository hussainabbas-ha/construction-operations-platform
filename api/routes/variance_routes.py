"""
Variance Detection routes — Phase 3.

Three endpoints, all read from existing DB tables (no HCSS calls):

  GET /api/variance/budget      — budget vs actual, cost variance per cost code
  GET /api/variance/quantity  — planned vs installed, quantity variance per cost code
  GET /api/variance/billing   — stub, not yet implemented

Date input (all endpoints):
  ?date=YYYY-MM-DD                    single day
  ?start_date=YYYY-MM-DD&end_date=YYYY-MM-DD   date range (max 90 days)

Optional filters vary per endpoint — see individual docstrings.
"""
from __future__ import annotations

import logging
import uuid
from datetime import date as date_type, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from api.database import get_db
from api.schemas import ErrorResponse
from variance_detection.detector import get_cost_variance, get_quantity_variance
from variance_detection.schemas import (
    BillingVarianceResponse,
    CostVarianceItemSchema,
    CostVarianceResponse,
    CostVarianceSummary,
    QuantityVarianceItemSchema,
    QuantityVarianceResponse,
    QuantityVarianceSummary,
)

router = APIRouter(prefix="/variance", tags=["Variance Detection"])
logger = logging.getLogger(__name__)


# ── Shared helpers ────────────────────────────────────────────────────────────

def _yesterday() -> str:
    return (date_type.today() - timedelta(days=1)).strftime("%Y-%m-%d")


def _validate_date(value: str, label: str) -> str:
    try:
        date_type.fromisoformat(value)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid {label} '{value}'. Expected format: YYYY-MM-DD.",
        )
    return value


def _resolve_period(
    date: str | None,
    start_date: str | None,
    end_date: str | None,
) -> tuple[str, str]:
    """
    Resolve query params to (start, end) pair.

    Priority:
      1. date          → single day  (start == end)
      2. start_date + end_date → explicit range
      3. neither       → yesterday  (start == end)
    """
    if date:
        d = _validate_date(date, "date")
        return d, d

    if start_date or end_date:
        if not start_date or not end_date:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Provide both start_date and end_date for a range query.",
            )
        sd = _validate_date(start_date, "start_date")
        ed = _validate_date(end_date, "end_date")
        if ed < sd:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="end_date must be on or after start_date.",
            )
        delta = (date_type.fromisoformat(ed) - date_type.fromisoformat(sd)).days
        if delta > 90:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Date range cannot exceed 90 days.",
            )
        return sd, ed

    # Default: yesterday
    y = _yesterday()
    return y, y


# ── Route 1: Cost Variance ────────────────────────────────────────────────────

@router.get(
    "/budget",
    response_model=CostVarianceResponse,
    summary="Cost variance — budget vs actual per cost code",
    description=(
        "Reads from `budget_tracking_results` and computes:\n\n"
        "- **cost_variance_amount** = `expected_budget − actual_cost`  "
        "(positive = budget remaining, negative = over budget)\n"
        "- **cost_variance_pct** = `(actual_cost / expected_budget) × 100`  "
        "(utilization %)\n\n"
        "**Risk threshold:** utilization ≥ 75 % → `OVER_RISK`\n\n"
        "When a date range is supplied, actual costs are summed across all dates "
        "per cost code while the expected budget is taken from the latest record."
    ),
    responses={
        404: {"description": "No budget data found for the requested period"},
        422: {"model": ErrorResponse, "description": "Invalid date or filter value"},
    },
)
def cost_variance(
    date:          str | None = Query(default=None, description="Single date YYYY-MM-DD (default: yesterday)"),
    start_date:    str | None = Query(default=None, description="Range start YYYY-MM-DD"),
    end_date:      str | None = Query(default=None, description="Range end YYYY-MM-DD"),
    job_id:        str | None = Query(default=None, description="Filter by HCSS job UUID"),
    business_unit: str | None = Query(default=None, description="Filter by business unit name"),
    cost_code:     str | None = Query(default=None, description="Filter by cost code, e.g. '01-100'"),
    status_filter: str | None = Query(
        default=None,
        alias="status",
        description="Filter by status: ON_TRACK | OVER_RISK",
    ),
    db: Session = Depends(get_db),
) -> CostVarianceResponse:
    request_id = str(uuid.uuid4())
    sd, ed = _resolve_period(date, start_date, end_date)

    logger.info(
        "Cost variance request: %s to %s job_id=%s bu=%s cc=%s status=%s [%s]",
        sd, ed, job_id, business_unit, cost_code, status_filter, request_id,
    )

    items, summary = get_cost_variance(
        db, sd, ed,
        job_id=job_id,
        business_unit=business_unit,
        cost_code=cost_code,
        status_filter=status_filter,
    )

    if not items:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                f"No budget data found for {sd} to {ed}. "
                "Data is synced every 8 hours by the background job."
            ),
        )

    logger.info(
        "Cost variance response: %s to %s items=%d at_risk=%d [%s]",
        sd, ed, summary.total_items, summary.at_risk, request_id,
    )

    return CostVarianceResponse(
        summary=CostVarianceSummary(
            period_start=summary.period_start,
            period_end=summary.period_end,
            total_items=summary.total_items,
            at_risk=summary.at_risk,
            total_expected_budget=summary.total_expected_budget,
            total_actual_cost=summary.total_actual_cost,
            total_cost_variance=summary.total_cost_variance,
        ),
        items=[
            CostVarianceItemSchema(
                cost_code_id=i.cost_code_id,
                cost_code=i.cost_code,
                cost_code_description=i.cost_code_description,
                job_id=i.job_id,
                job_name=i.job_name,
                business_unit=i.business_unit,
                budgeted_all_cost=i.budgeted_all_cost,
                expected_budget=i.expected_budget,
                actual_cost=i.actual_cost,
                cost_variance_amount=i.cost_variance_amount,
                cost_variance_pct=i.cost_variance_pct,
                loss_amount=i.loss_amount,
                loss_pct=i.loss_pct,
                status=i.status,
            )
            for i in items
        ],
    )


# ── Route 2: Quantity Variance ────────────────────────────────────────────────

@router.get(
    "/quantity",
    response_model=QuantityVarianceResponse,
    summary="Quantity variance — planned vs installed per cost code",
    description=(
        "Reads from `quantity_tracking_results` and computes:\n\n"
        "- **qty_variance_amount** = `planned_quantity − installed_quantity`  "
        "(positive = quantity remaining, negative = over-run)\n"
        "- **qty_variance_pct** = `(installed_quantity / planned_quantity) × 100`  "
        "(percent complete)\n\n"
        "**Risk thresholds:**\n"
        "- ≥ 75 % → `NEAR_COMPLETION`\n"
        "- ≥ 100 % → `OVER_RISK`\n\n"
        "When a date range is supplied, installed quantities are summed across all "
        "dates per cost code while planned quantity is taken from the latest record."
    ),
    responses={
        404: {"description": "No quantity data found for the requested period"},
        422: {"model": ErrorResponse, "description": "Invalid date or filter value"},
    },
)
def quantity_variance(
    date:          str | None = Query(default=None, description="Single date YYYY-MM-DD (default: yesterday)"),
    start_date:    str | None = Query(default=None, description="Range start YYYY-MM-DD"),
    end_date:      str | None = Query(default=None, description="Range end YYYY-MM-DD"),
    job_id:        str | None = Query(default=None, description="Filter by HCSS job UUID"),
    cost_type:     str | None = Query(
        default=None,
        description="Filter by cost type: self_perform | subcontractor",
    ),
    status_filter: str | None = Query(
        default=None,
        alias="status",
        description="Filter by status: ON_TRACK | NEAR_COMPLETION | OVER_RISK",
    ),
    db: Session = Depends(get_db),
) -> QuantityVarianceResponse:
    request_id = str(uuid.uuid4())
    sd, ed = _resolve_period(date, start_date, end_date)

    logger.info(
        "Quantity variance request: %s to %s job_id=%s cost_type=%s status=%s [%s]",
        sd, ed, job_id, cost_type, status_filter, request_id,
    )

    items, summary = get_quantity_variance(
        db, sd, ed,
        job_id=job_id,
        cost_type=cost_type,
        status_filter=status_filter,
    )

    if not items:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                f"No quantity data found for {sd} to {ed}. "
                "Data is synced every 8 hours by the background job."
            ),
        )

    logger.info(
        "Quantity variance response: %s to %s items=%d near=%d over=%d [%s]",
        sd, ed, summary.total_items, summary.near_completion, summary.over_risk, request_id,
    )

    return QuantityVarianceResponse(
        summary=QuantityVarianceSummary(
            period_start=summary.period_start,
            period_end=summary.period_end,
            total_items=summary.total_items,
            at_risk=summary.at_risk,
            near_completion=summary.near_completion,
            over_risk=summary.over_risk,
        ),
        items=[
            QuantityVarianceItemSchema(
                cost_code_id=i.cost_code_id,
                cost_code=i.cost_code,
                description=i.description,
                job_id=i.job_id,
                job_code=i.job_code,
                unit=i.unit,
                cost_type=i.cost_type,
                planned_quantity=i.planned_quantity,
                installed_quantity=i.installed_quantity,
                remaining_quantity=i.remaining_quantity,
                qty_variance_amount=i.qty_variance_amount,
                qty_variance_pct=i.qty_variance_pct,
                status=i.status,
                alert=i.alert,
            )
            for i in items
        ],
    )


# ── Route 3: Billing Variance (stub) ─────────────────────────────────────────

@router.get(
    "/billing",
    response_model=BillingVarianceResponse,
    summary="Billing variance — stub (not yet implemented)",
    description=(
        "Placeholder for the billing variance module.\n\n"
        "Always returns an empty `items` list with `not_implemented: true` "
        "until the billing data source is connected."
    ),
)
def billing_variance(
    date:       str | None = Query(default=None, description="Single date YYYY-MM-DD (default: yesterday)"),
    start_date: str | None = Query(default=None, description="Range start YYYY-MM-DD"),
    end_date:   str | None = Query(default=None, description="Range end YYYY-MM-DD"),
) -> BillingVarianceResponse:
    sd, ed = _resolve_period(date, start_date, end_date)
    logger.info("Billing variance stub called: %s to %s", sd, ed)
    return BillingVarianceResponse(period_start=sd, period_end=ed)
