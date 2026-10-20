"""
api/routes/projection_routes.py

Projection Tracking API routes.

Endpoints:
  GET    /api/projection/count
  GET    /api/projection/verify
  POST   /api/projection/notify
  DELETE /api/projection/cache/clear
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, EmailStr, Field

from api.database import SessionLocal
from api.schemas import ErrorResponse
from notifications.projection_email_template import build_projection_email_body
from notifications.budget_email_sender import send_budget_email

router = APIRouter(prefix="/projection", tags=["Projection Tracking"])
logger = logging.getLogger(__name__)


class ProjectionCountResponse(BaseModel):
    period_start: str
    period_end: str
    total: int
    on_track: int
    at_risk: int
    over_budget: int
    alerts: int


class ProjectionItemRow(BaseModel):
    period_start: str = ""
    period_end: str = ""

    job_id: str
    job_code: str
    job_name: str
    business_unit: str | None = None

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

    projected_final: float = 0.0
    projected_over_under: float = 0.0
    performance_factor: float = 0.0

    actual_labor_cost: float = 0.0
    actual_equipment_cost: float = 0.0
    actual_material_cost: float = 0.0
    actual_subcontract_cost: float = 0.0
    actual_trucking_cost: float = 0.0

    quantity_from_job_costs: float = 0.0
    labor_hours: float = 0.0
    equipment_hours: float = 0.0

    status: str = Field(..., description="ON_TRACK | AT_RISK | OVER_BUDGET")
    alert: bool
    discrepancy_flag: bool


class ProjectionVerifyResponse(BaseModel):
    period_start: str
    period_end: str
    total: int
    on_track: int
    at_risk: int
    over_budget: int
    alerts: int
    items: list[ProjectionItemRow]


class ProjectionNotifyRequest(BaseModel):
    month: str | None = Field(default=None, description="Single month YYYY-MM")
    start_month: str | None = Field(default=None, description="Range start YYYY-MM")
    end_month: str | None = Field(default=None, description="Range end YYYY-MM")
    start_date: str | None = Field(default=None, description="Exact period start YYYY-MM-DD")
    end_date: str | None = Field(default=None, description="Exact period end YYYY-MM-DD")
    job_id: str | None = Field(default=None, description="Filter by HCSS job UUID")
    job_code: str | None = Field(default=None, description="Filter by job code")
    cost_code_id: str | None = Field(default=None, description="Filter by HCSS cost code UUID")
    cost_code: str | None = Field(default=None, description="Filter by cost code")
    business_unit: str | None = Field(default=None, description="Filter by business unit")
    status_filter: str | None = Field(default=None, description="ON_TRACK | AT_RISK | OVER_BUDGET")
    recipients: list[EmailStr] = Field(..., min_length=1)
    comments: str = Field(default="")


class ProjectionNotifyResponse(BaseModel):
    period_start: str
    period_end: str
    recipients_count: int
    items_included: int
    message: str


def _validate_month(month_str: str, label: str) -> str:
    try:
        datetime.strptime(month_str, "%Y-%m")
    except ValueError:
        raise HTTPException(status_code=422, detail=f"Invalid {label} '{month_str}'. Expected YYYY-MM.")
    return month_str


def _validate_date(date_str: str, label: str) -> str:
    try:
        datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(status_code=422, detail=f"Invalid {label} '{date_str}'. Expected YYYY-MM-DD.")
    return date_str


def _current_month() -> str:
    return datetime.now().strftime("%Y-%m")


def _month_to_date_range(month: str) -> tuple[str, str]:
    import calendar
    year, mon = int(month[:4]), int(month[5:7])
    last_day = calendar.monthrange(year, mon)[1]
    return f"{month}-01", f"{month}-{last_day:02d}"


def _resolve_period(
    month: str | None,
    start_month: str | None,
    end_month: str | None,
    start_date: str | None = None,
    end_date: str | None = None,
) -> tuple[str, str, str, str]:
    """
    Returns:
        (cache_mode, cache_start, period_start_date, period_end_date)

    cache_mode:
        "month"  -> cache_start is YYYY-MM
        "period" -> db_services builds YYYY-MM-DD__YYYY-MM-DD key
    """
    if start_date or end_date:
        if not start_date or not end_date:
            raise HTTPException(status_code=422, detail="Provide both start_date and end_date.")
        sd = _validate_date(start_date, "start_date")
        ed = _validate_date(end_date, "end_date")
        if ed < sd:
            raise HTTPException(status_code=422, detail="end_date must be after start_date.")
        return "period", sd, sd, ed

    if month:
        m = _validate_month(month, "month")
        sd, ed = _month_to_date_range(m)
        return "month", m, sd, ed

    if start_month or end_month:
        if not start_month or not end_month:
            raise HTTPException(status_code=422, detail="Provide both start_month and end_month.")
        sm = _validate_month(start_month, "start_month")
        em = _validate_month(end_month, "end_month")
        if em < sm:
            raise HTTPException(status_code=422, detail="end_month must be after start_month.")
        sd, _ = _month_to_date_range(sm)
        _, ed = _month_to_date_range(em)
        return "period", sd, sd, ed

    cm = _current_month()
    sd, ed = _month_to_date_range(cm)
    return "month", cm, sd, ed


def _aggregate_projections(raw: list[dict]) -> list[dict]:
    seen: dict[tuple[str, str, str], dict] = {}
    for item in raw:
        key = (
            item.get("period_start", ""),
            item.get("job_id", ""),
            item.get("cost_code_id", ""),
        )
        if key[1] and key[2]:
            seen[key] = item
    return list(seen.values())


def _fetch_for_range(
    cache_mode: str,
    cache_start: str,
    period_start: str,
    period_end: str,
    job_id: str | None = None,
    job_code: str | None = None,
    cost_code_id: str | None = None,
    cost_code: str | None = None,
    business_unit: str | None = None,
    status_filter: str | None = None,
    request_id: str = "-",
) -> list[dict]:
    from api.db_services import (
        get_or_fetch_projection_for_month,
        get_or_fetch_projection_for_period,
    )

    db = SessionLocal()
    try:
        if cache_mode == "month":
            raw = get_or_fetch_projection_for_month(db, cache_start)
        else:
            raw = get_or_fetch_projection_for_period(db, period_start, period_end)
    finally:
        db.close()

    if not raw:
        raise HTTPException(
            status_code=404,
            detail=f"No projection data available for {period_start} to {period_end}.",
        )

    if job_id:
        raw = [r for r in raw if r.get("job_id") == job_id]
    if job_code:
        raw = [r for r in raw if str(r.get("job_code", "")).strip() == str(job_code).strip()]
    if cost_code_id:
        raw = [r for r in raw if r.get("cost_code_id") == cost_code_id]
    if cost_code:
        raw = [r for r in raw if str(r.get("cost_code", "")).strip() == str(cost_code).strip()]
    if business_unit:
        raw = [r for r in raw if r.get("business_unit") == business_unit]
    if status_filter:
        raw = [r for r in raw if r.get("status") == status_filter.upper()]

    aggregated = _aggregate_projections(raw)
    logger.info("Projection fetch: %s to %s total=%d request_id=%s", period_start, period_end, len(aggregated), request_id)
    return aggregated


def _counts(items: list[dict]) -> dict[str, int]:
    return {
        "total": len(items),
        "on_track": sum(1 for i in items if i.get("status") == "ON_TRACK"),
        "at_risk": sum(1 for i in items if i.get("status") == "AT_RISK"),
        "over_budget": sum(1 for i in items if i.get("status") == "OVER_BUDGET"),
        "alerts": sum(1 for i in items if i.get("alert", False)),
    }


@router.get(
    "/count",
    response_model=ProjectionCountResponse,
    summary="Projection summary counts",
    responses={404: {"description": "No projection data found"}, 502: {"model": ErrorResponse}},
)
def projection_count(
    month: str | None = Query(default=None),
    start_month: str | None = Query(default=None),
    end_month: str | None = Query(default=None),
    start_date: str | None = Query(default=None),
    end_date: str | None = Query(default=None),
    job_id: str | None = Query(default=None),
    job_code: str | None = Query(default=None),
    cost_code_id: str | None = Query(default=None),
    cost_code: str | None = Query(default=None),
    business_unit: str | None = Query(default=None),
    status_filter: str | None = Query(default=None, alias="status"),
) -> ProjectionCountResponse:
    request_id = str(uuid.uuid4())
    cache_mode, cache_start, ps, pe = _resolve_period(month, start_month, end_month, start_date, end_date)
    items = _fetch_for_range(cache_mode, cache_start, ps, pe, job_id, job_code, cost_code_id, cost_code, business_unit, status_filter, request_id)
    c = _counts(items)
    return ProjectionCountResponse(
        period_start=ps,
        period_end=pe,
        total=c["total"],
        on_track=c["on_track"],
        at_risk=c["at_risk"],
        over_budget=c["over_budget"],
        alerts=c["alerts"],
    )


@router.get(
    "/verify",
    response_model=ProjectionVerifyResponse,
    summary="Full Projection / Production Analysis detail",
    responses={404: {"description": "No projection data found"}, 502: {"model": ErrorResponse}},
)
def projection_verify(
    month: str | None = Query(default=None),
    start_month: str | None = Query(default=None),
    end_month: str | None = Query(default=None),
    start_date: str | None = Query(default=None),
    end_date: str | None = Query(default=None),
    job_id: str | None = Query(default=None),
    job_code: str | None = Query(default=None),
    cost_code_id: str | None = Query(default=None),
    cost_code: str | None = Query(default=None),
    business_unit: str | None = Query(default=None),
    status_filter: str | None = Query(default=None, alias="status"),
) -> ProjectionVerifyResponse:
    request_id = str(uuid.uuid4())
    cache_mode, cache_start, ps, pe = _resolve_period(month, start_month, end_month, start_date, end_date)
    items = _fetch_for_range(cache_mode, cache_start, ps, pe, job_id, job_code, cost_code_id, cost_code, business_unit, status_filter, request_id)
    c = _counts(items)
    logger.info("Projection verify: %s to %s returning %d items", ps, pe, len(items))
    return ProjectionVerifyResponse(
        period_start=ps,
        period_end=pe,
        total=c["total"],
        on_track=c["on_track"],
        at_risk=c["at_risk"],
        over_budget=c["over_budget"],
        alerts=c["alerts"],
        items=[ProjectionItemRow(**i) for i in items],
    )


@router.post(
    "/notify",
    response_model=ProjectionNotifyResponse,
    summary="Send projection report email on demand",
    responses={422: {"model": ErrorResponse}, 502: {"model": ErrorResponse}},
)
def projection_notify(payload: ProjectionNotifyRequest) -> ProjectionNotifyResponse:
    request_id = str(uuid.uuid4())
    cache_mode, cache_start, ps, pe = _resolve_period(payload.month, payload.start_month, payload.end_month, payload.start_date, payload.end_date)

    items = _fetch_for_range(
        cache_mode,
        cache_start,
        ps,
        pe,
        payload.job_id,
        payload.job_code,
        payload.cost_code_id,
        payload.cost_code,
        payload.business_unit,
        payload.status_filter,
        request_id,
    )

    item_rows = [ProjectionItemRow(**i) for i in items]
    subject, html_body = build_projection_email_body(
        tracking_month=ps if ps == pe else f"{ps} to {pe}",
        items=item_rows,
        comments=payload.comments,
        triggered_by="Manual",
    )

    recipient_list = [str(r) for r in payload.recipients]
    try:
        send_budget_email(subject=subject, html_body=html_body, recipients=recipient_list)
    except ValueError as exc:
        raise HTTPException(status_code=502, detail=f"Email config error: {exc}") from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Email send failed: {exc}") from exc

    return ProjectionNotifyResponse(
        period_start=ps,
        period_end=pe,
        recipients_count=len(recipient_list),
        items_included=len(item_rows),
        message=f"Projection report for {ps} to {pe} sent to {len(recipient_list)} recipient(s) with {len(item_rows)} row(s).",
    )


@router.delete(
    "/cache/clear",
    summary="Clear projection cache for months or exact date ranges",
    tags=["Projection Tracking"],
)
def clear_projection_cache(
    months: str | None = Query(default=None, description="Comma-separated months e.g. 2026-03,2026-04"),
    start_date: str | None = Query(default=None, description="Exact start date YYYY-MM-DD"),
    end_date: str | None = Query(default=None, description="Exact end date YYYY-MM-DD"),
) -> dict:
    from api.database import SessionLocal, ProjectionTrackingCache, ProjectionTrackingResult

    keys: list[str] = []

    if months:
        for m in [x.strip() for x in months.split(",") if x.strip()]:
            _validate_month(m, "month")
            keys.append(m)

    if start_date or end_date:
        if not start_date or not end_date:
            raise HTTPException(status_code=422, detail="Provide both start_date and end_date.")
        sd = _validate_date(start_date, "start_date")
        ed = _validate_date(end_date, "end_date")
        if ed < sd:
            raise HTTPException(status_code=422, detail="end_date must be after start_date.")
        keys.append(sd if sd == ed else f"{sd}__{ed}")

    if not keys:
        raise HTTPException(status_code=422, detail="Provide months or start_date/end_date.")

    db = SessionLocal()
    deleted_results = 0
    deleted_cache = 0
    try:
        for key in keys:
            deleted_results += db.query(ProjectionTrackingResult).filter(
                ProjectionTrackingResult.tracking_month == key
            ).delete()
            deleted_cache += db.query(ProjectionTrackingCache).filter(
                ProjectionTrackingCache.tracking_month == key
            ).delete()
        db.commit()
        return {
            "cleared_keys": keys,
            "deleted_cache_entries": deleted_cache,
            "deleted_result_rows": deleted_results,
            "message": "Cache cleared. Next API call will re-fetch from HCSS.",
        }
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Failed: {exc}")
    finally:
        db.close()