"""
Quantity Test routes — isolated daily-sum read model.

Reads ONLY from `quantity_test` (populated by scripts/populate_quantity_test.py).
Does NOT call HCSS, does NOT touch quantity_tracking_* production tables,
and does NOT share logic with /api/quantity/* production routes.

Same query params as production quantity verify/count:
  ?date=YYYY-MM-DD
  or ?start_date=YYYY-MM-DD&end_date=YYYY-MM-DD
"""
from __future__ import annotations

import logging
from datetime import date as date_type, timedelta

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from api.database import SessionLocal
from api.db_services import (
    get_quantity_test_days_present,
    sum_quantity_test_for_period,
)
from api.schemas import ErrorResponse

router = APIRouter(prefix="/quantity-test", tags=["Quantity Test"])
logger = logging.getLogger(__name__)


# ── Schemas (mirror production quantity response shape) ───────────────────────

class QuantityTestItemRow(BaseModel):
    cost_code_id: str
    cost_code: str
    description: str
    job_id: str
    job_code: str
    unit: str
    cost_type: str = Field(..., description="self_perform | subcontractor")
    planned_quantity: float
    installed_quantity: float
    remaining_quantity: float
    variance: float = Field(
        ...,
        description="planned_quantity - installed_quantity",
    )
    percent_complete: float
    status: str = Field(..., description="ON_TRACK | NEAR_COMPLETION | OVER_RISK")
    alert: bool


class QuantityTestCountResponse(BaseModel):
    period_start: str
    period_end: str
    total: int
    on_track: int
    near_completion: int
    over_risk: int
    days_requested: int
    days_present: int
    missing_days: list[str]


class QuantityTestVerifyResponse(BaseModel):
    period_start: str
    period_end: str
    total: int
    days_requested: int
    days_present: int
    missing_days: list[str]
    source: str = "quantity_test"
    items: list[QuantityTestItemRow]


# ── Helpers ───────────────────────────────────────────────────────────────────

def _validate_date(date_str: str, label: str) -> str:
    try:
        date_type.fromisoformat(date_str)
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail=f"Invalid {label} '{date_str}'. Expected YYYY-MM-DD.",
        )
    return date_str


def _resolve_period(date, start_date, end_date):
    if date:
        d = _validate_date(date, "date")
        return d, d
    if start_date or end_date:
        if not start_date or not end_date:
            raise HTTPException(
                status_code=422,
                detail="Provide both start_date and end_date, or use date for a single day.",
            )
        return (
            _validate_date(start_date, "start_date"),
            _validate_date(end_date, "end_date"),
        )
    yesterday = (date_type.today() - timedelta(days=1)).strftime("%Y-%m-%d")
    return yesterday, yesterday


def _date_list(start_date: str, end_date: str) -> list[str]:
    from datetime import datetime, timedelta as td

    dates, cur = [], datetime.fromisoformat(start_date)
    end = datetime.fromisoformat(end_date)
    while cur <= end:
        dates.append(cur.strftime("%Y-%m-%d"))
        cur += td(days=1)
    return dates


def _to_row(item: dict) -> QuantityTestItemRow:
    planned = float(item["planned_quantity"])
    installed = float(item["installed_quantity"])
    return QuantityTestItemRow(
        cost_code_id=item["cost_code_id"],
        cost_code=item["cost_code"],
        description=item["description"],
        job_id=item["job_id"],
        job_code=item["job_code"],
        unit=item["unit"],
        cost_type=item["cost_type"],
        planned_quantity=planned,
        installed_quantity=installed,
        remaining_quantity=float(item.get("remaining_quantity", planned - installed)),
        variance=round(planned - installed, 4),
        percent_complete=float(item["percent_complete"]),
        status=item["status"],
        alert=bool(item["alert"]),
    )


def _load_period(sd: str, ed: str) -> tuple[list[dict], list[str], list[str]]:
    """
    Sum daily quantity_test rows for the period.
    Returns (aggregated_items, requested_days, missing_days).
    """
    requested = _date_list(sd, ed)
    db = SessionLocal()
    try:
        present = get_quantity_test_days_present(db, sd, ed)
        present_set = set(present)
        missing = [d for d in requested if d not in present_set]
        raw = sum_quantity_test_for_period(db, sd, ed)
    finally:
        db.close()
    return raw, requested, missing


# ── Routes ────────────────────────────────────────────────────────────────────

@router.get(
    "/count",
    response_model=QuantityTestCountResponse,
    summary="Quantity-test summary counts (sum of daily quantity_test rows)",
    responses={404: {"description": "No data"}, 502: {"model": ErrorResponse}},
)
def quantity_test_count(
    date: str | None = Query(default=None, description="Single date YYYY-MM-DD"),
    start_date: str | None = Query(default=None),
    end_date: str | None = Query(default=None),
) -> QuantityTestCountResponse:
    sd, ed = _resolve_period(date, start_date, end_date)
    results, requested, missing = _load_period(sd, ed)
    if not results:
        raise HTTPException(
            status_code=404,
            detail=(
                f"No quantity_test data for {sd} to {ed}. "
                f"Run: python scripts/populate_quantity_test.py "
                f"--start {sd} --end {ed}"
            ),
        )
    total = len(results)
    on_track = sum(1 for r in results if r["status"] == "ON_TRACK")
    near = sum(1 for r in results if r["status"] == "NEAR_COMPLETION")
    over = sum(1 for r in results if r["status"] == "OVER_RISK")
    logger.info(
        "Quantity-test count: %s to %s total=%d days_present=%d missing=%d",
        sd, ed, total, len(requested) - len(missing), len(missing),
    )
    return QuantityTestCountResponse(
        period_start=sd,
        period_end=ed,
        total=total,
        on_track=on_track,
        near_completion=near,
        over_risk=over,
        days_requested=len(requested),
        days_present=len(requested) - len(missing),
        missing_days=missing,
    )


@router.get(
    "/verify",
    response_model=QuantityTestVerifyResponse,
    summary="Quantity-test rows (sum daily quantity_test by job + cost code)",
    responses={404: {"description": "No data"}, 502: {"model": ErrorResponse}},
)
def quantity_test_verify(
    date: str | None = Query(default=None, description="Single date YYYY-MM-DD"),
    start_date: str | None = Query(default=None),
    end_date: str | None = Query(default=None),
    cost_type: str | None = Query(
        default=None, description="self_perform | subcontractor"
    ),
    status_filter: str | None = Query(
        default=None,
        alias="status",
        description="ON_TRACK | NEAR_COMPLETION | OVER_RISK",
    ),
) -> QuantityTestVerifyResponse:
    sd, ed = _resolve_period(date, start_date, end_date)
    results, requested, missing = _load_period(sd, ed)
    if not results:
        raise HTTPException(
            status_code=404,
            detail=(
                f"No quantity_test data for {sd} to {ed}. "
                f"Run: python scripts/populate_quantity_test.py "
                f"--start {sd} --end {ed}"
            ),
        )
    if cost_type:
        results = [r for r in results if r["cost_type"] == cost_type.lower()]
    if status_filter:
        results = [r for r in results if r["status"] == status_filter.upper()]

    logger.info(
        "Quantity-test verify: %s to %s returning %d items "
        "(days_present=%d missing=%d)",
        sd, ed, len(results),
        len(requested) - len(missing), len(missing),
    )
    return QuantityTestVerifyResponse(
        period_start=sd,
        period_end=ed,
        total=len(results),
        days_requested=len(requested),
        days_present=len(requested) - len(missing),
        missing_days=missing,
        source="quantity_test",
        items=[_to_row(r) for r in results],
    )
