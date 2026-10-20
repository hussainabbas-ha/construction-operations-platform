"""
Billing Draft routes.

GET  /api/billing-draft?period=YYYY-MM[&job_code=XXXX]   — read stored rows
POST /api/billing-draft/build?period=YYYY-MM             — (re)build + store a month

Rows are flat: one per (job_code, cost_code). Querying by job_code returns all of
that job's cost-code rows for the month — mirroring the Budget grid.
"""
from __future__ import annotations

import logging
import re

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from api.database import SessionLocal

router = APIRouter(prefix="/billing-draft", tags=["Billing Draft"])
logger = logging.getLogger(__name__)

_PERIOD_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


def _validate_period(period: str) -> str:
    if not period or not _PERIOD_RE.match(period):
        raise HTTPException(status_code=422, detail=f"Invalid period {period!r}. Expected 'YYYY-MM'.")
    return period


class BillingDraftRow(BaseModel):
    period: str
    job_id: str | None = None
    job_code: str
    job_description: str | None = ""
    cost_code_id: str | None = None
    cost_code: str
    description: str | None = ""
    unit: str | None = ""
    budgeted_quantity: float = 0.0
    actual_quantity: float = 0.0
    percent_complete: float = 0.0
    budgeted_all_cost: float = 0.0
    expected_all_cost: float = 0.0
    capped_expected_all_cost: float = 0.0
    actual_all_cost: float = 0.0
    variance: float = 0.0
    capped_variance: float = 0.0


class BillingDraftResponse(BaseModel):
    period: str
    job_code: str | None = None
    count: int
    results: list[BillingDraftRow]


class BuildResponse(BaseModel):
    period: str
    rows_stored: int
    message: str


@router.get("", response_model=BillingDraftResponse, summary="Get stored billing draft rows")
def get_billing_draft_rows(
    period: str = Query(..., description="Billing month 'YYYY-MM'"),
    job_code: str | None = Query(default=None, description="Filter to one job code"),
) -> BillingDraftResponse:
    _validate_period(period)
    from api.db_services import get_billing_draft

    db = SessionLocal()
    try:
        rows = get_billing_draft(db, period, job_code)
    finally:
        db.close()

    if not rows:
        raise HTTPException(
            status_code=404,
            detail=f"No billing draft data for period {period}"
                   + (f", job_code {job_code}" if job_code else "")
                   + ". Build it first via POST /api/billing-draft/build.",
        )
    return BillingDraftResponse(period=period, job_code=job_code, count=len(rows),
                                results=[BillingDraftRow(**r) for r in rows])


@router.post("/build", response_model=BuildResponse, summary="Build + store a month's billing draft")
def build_billing_draft(
    period: str = Query(..., description="Billing month 'YYYY-MM' to (re)build"),
) -> BuildResponse:
    _validate_period(period)
    from billing_draft_tracking.builder import build_billing_draft_rows
    from api.db_services import store_billing_draft

    try:
        rows = build_billing_draft_rows(period)
    except Exception as exc:
        logger.exception("Billing draft build failed for %s", period)
        raise HTTPException(status_code=502, detail=f"Build failed: {exc}") from exc

    db = SessionLocal()
    try:
        stored = store_billing_draft(db, period, rows)
    finally:
        db.close()

    return BuildResponse(period=period, rows_stored=stored,
                         message=f"Billing draft for {period} built and stored ({stored} rows).")


# ── AIA Application & Certificate for Payment (Gemini) ─────────────────────

def _next_application_no(prior: dict | None) -> str:
    if not prior:
        return "001 (draft)"
    import re as _re
    m = _re.match(r"\s*(\d+)", str(prior.get("application_no") or "0"))
    nxt = (int(m.group(1)) if m else 1) + 1
    return f"{nxt:03d} (draft)"


class AIALineItem(BaseModel):
    cost_code: str
    description: str | None = ""
    unit: str | None = ""
    scheduled_value: float = 0.0
    budgeted_quantity: float = 0.0
    actual_quantity: float = 0.0
    percent_complete: float = 0.0
    earned_value_capped: float = 0.0
    completed_this_period: float = 0.0
    billed: bool = False
    hold_reason: str | None = None


class AIASummary(BaseModel):
    original_contract_sum: float = 0.0
    net_change_by_co: float = 0.0
    contract_sum_to_date: float = 0.0
    total_completed_stored_to_date: float = 0.0
    retainage_pct: float = 0.0
    total_retainage: float = 0.0
    total_earned_less_retainage: float = 0.0
    less_previous_certificates: float = 0.0
    current_payment_due: float = 0.0
    gross_completed_this_period: float = 0.0
    remaining_contract_balance: float = 0.0
    items_billed: int = 0
    items_held: int = 0


class AIAProject(BaseModel):
    job_code: str
    job_id: str | None = None
    job_description: str | None = ""


class AIADetailResponse(BaseModel):
    """Screen 2 — full AIA draft for the modal."""
    period: str
    project: AIAProject
    application_no: str | None = None
    period_to: str | None = None
    prepared_date: str | None = None
    summary: AIASummary
    assumptions: list[str] = Field(default_factory=list)
    line_items: list[AIALineItem] = Field(default_factory=list)


class AIAListItem(BaseModel):
    """Screen 1 — one row per job (table + action button to open the modal)."""
    period: str
    job_code: str
    job_description: str | None = ""
    application_no: str | None = None
    period_to: str | None = None
    prepared_date: str | None = None
    original_contract_sum: float = 0.0
    total_completed_stored_to_date: float = 0.0
    current_payment_due: float = 0.0
    items_billed: int = 0
    items_held: int = 0


class AIAListResponse(BaseModel):
    period: str
    count: int
    jobs: list[AIAListItem]


@router.get("/aia/list", response_model=AIAListResponse,
            summary="Screen 1 — list of jobs with an AIA draft for the month")
def list_aia(period: str = Query(..., description="Billing month 'YYYY-MM'")):
    _validate_period(period)
    from api.db_services import get_billing_draft_aia
    db = SessionLocal()
    try:
        rows = get_billing_draft_aia(db, period)
    finally:
        db.close()
    jobs = [
        AIAListItem(
            period=period,
            job_code=a["project"]["job_code"],
            job_description=a["project"].get("job_description") or "",
            application_no=a.get("application_no"),
            period_to=a.get("period_to"),
            prepared_date=a.get("prepared_date"),
            original_contract_sum=a["summary"].get("original_contract_sum", 0.0),
            total_completed_stored_to_date=a["summary"].get("total_completed_stored_to_date", 0.0),
            current_payment_due=a["summary"].get("current_payment_due", 0.0),
            items_billed=a["summary"].get("items_billed", 0),
            items_held=a["summary"].get("items_held", 0),
        )
        for a in rows
    ]
    return AIAListResponse(period=period, count=len(jobs), jobs=jobs)


@router.get("/aia/detail", response_model=AIADetailResponse,
            summary="Screen 2 — full AIA draft for one job (modal)")
def detail_aia(
    period: str = Query(..., description="Billing month 'YYYY-MM'"),
    job_code: str = Query(..., description="Job code"),
):
    _validate_period(period)
    from api.db_services import get_billing_draft_aia
    db = SessionLocal()
    try:
        rows = get_billing_draft_aia(db, period, job_code)
    finally:
        db.close()
    if not rows:
        raise HTTPException(status_code=404,
            detail=f"No AIA draft for {period} / {job_code}. Build it via POST /api/billing-draft/aia/build.")
    return AIADetailResponse(**rows[0])


@router.get("/aia", summary="Get stored AIA draft(s) for a period")
def get_aia(
    period: str = Query(..., description="Billing month 'YYYY-MM'"),
    job_code: str | None = Query(default=None, description="Filter to one job code"),
):
    _validate_period(period)
    from api.db_services import get_billing_draft_aia
    db = SessionLocal()
    try:
        out = get_billing_draft_aia(db, period, job_code)
    finally:
        db.close()
    if not out:
        raise HTTPException(status_code=404,
            detail=f"No AIA draft for {period}" + (f" / {job_code}" if job_code else "")
                   + ". Build it via POST /api/billing-draft/aia/build.")
    return {"period": period, "count": len(out), "results": out}


@router.post("/aia/build", summary="Generate + store the AIA draft for one job (Gemini)")
def build_aia(
    period: str = Query(..., description="Billing month 'YYYY-MM'"),
    job_code: str = Query(..., description="Job code to generate the AIA draft for"),
    retainage_pct: float = Query(default=5.0, description="Retainage percent (e.g. 5.0)"),
):
    _validate_period(period)
    from billing_draft_tracking.aia_generator import generate_aia_for_job
    from api.db_services import get_prior_aia, store_billing_draft_aia

    db = SessionLocal()
    try:
        prior = get_prior_aia(db, job_code, period)
    finally:
        db.close()
    prev_completed = float(prior["summary"]["total_completed_stored_to_date"]) if prior else 0.0

    try:
        aia = generate_aia_for_job(
            period, job_code,
            application_no=_next_application_no(prior),
            retainage_pct=retainage_pct / 100.0,
            previous_completed_to_date=prev_completed,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("AIA generation failed for %s/%s", period, job_code)
        raise HTTPException(status_code=502, detail=f"AIA generation failed: {exc}") from exc

    db = SessionLocal()
    try:
        store_billing_draft_aia(db, aia)
    finally:
        db.close()
    return aia
