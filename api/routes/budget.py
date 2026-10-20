"""
Budget Tracking routes - uses cache-aside via get_or_fetch_budget_for_date.
Single DB session per request. Falls back to HCSS on cache miss.
"""
from __future__ import annotations
import logging, uuid
from datetime import date as date_type, datetime, timedelta
from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, EmailStr, Field
from api.database import SessionLocal
from api.schemas import ErrorResponse
from budget_tracking.analyzer import BudgetResult
from notifications.budget_email_sender import send_budget_email
from notifications.budget_email_template import build_budget_email_body

router = APIRouter(prefix="/budget", tags=["Budget Tracking"])
logger = logging.getLogger(__name__)


# ── Schemas ───────────────────────────────────────────────────────────────────

class BudgetCountResponse(BaseModel):
    period_start: str
    period_end: str
    total: int
    on_track: int
    over_risk: int

class ForemanDetail(BaseModel):
    foreman_id: str
    foreman_name: str

class BudgetItemRow(BaseModel):
    cost_code_id: str
    cost_code: str
    cost_code_description: str
    job_id: str
    job_name: str
    business_unit: str
    # ── Budget values ──────────────────────────────────────────────────
    budgeted_all_cost: float = Field(
        ...,
        description="Total HCSS planned budget (all cost-type dollars). Raw stored value.",
    )
    expected_budget: float = Field(
        ...,
        description=(
            "(installed_quantity / planned_quantity) × budgeted_all_cost. "
            "Zero when no quantity data is available."
        ),
    )
    actual_cost: float
    utilization_percentage: int | None = Field(
        default=None,
        description="(actual_cost / expected_budget) × 100. null when expected_budget is 0.",
    )
    variance: float = Field(
        ...,
        description="expected_budget − actual_cost. Positive = under budget, negative = over.",
    )
    loss_amount: float = Field(
        default=0.0,
        description="max(0, actual_cost − expected_budget). 0 when under/on budget.",
    )
    loss_pct: float | None = Field(
        default=None,
        description=(
            "(loss_amount / expected_budget) × 100. "
            "0 when under/on budget. null when expected_budget is 0."
        ),
    )
    status: str = Field(..., description="ON_TRACK | OVER_RISK")
    # ── Actual cost breakdown by type ─────────────────────────────────
    labor_cost: float = Field(default=0.0)
    equipment_cost: float = Field(default=0.0)
    material_cost: float = Field(default=0.0)
    subcontract_cost: float = Field(default=0.0)
    trucking_cost: float = Field(default=0.0)
    labor_hours: float = Field(default=0.0)
    quantity: float = Field(default=0.0)
    # ── Budget breakdown by type ───────────────────────────────────────
    labor_budget: float = Field(default=0.0)
    equipment_budget: float = Field(default=0.0)
    material_budget: float = Field(default=0.0)
    subcontract_budget: float = Field(default=0.0)
    # ── Planned quantity from HCSS cost code record (info-only) ───────
    # Sourced from v1/costCodes — the same API call that fetches dollar budgets.
    # Included for verification against HCSS portal. Not used in any calculation.
    planned_quantity: float = Field(
        default=0.0,
        description=(
            "Total planned quantity from the HCSS cost code budget record "
            "(v1/costCodes). Info-only — not used in expected_budget math."
        ),
    )
    unit: str = Field(
        default="",
        description="Unit of measure for planned_quantity (e.g. LF, CY, EA).",
    )
    # ── Foremen who worked this cost code ─────────────────────────────
    foremen: list[ForemanDetail] = Field(default_factory=list)
    # ── Quantity completion (derived from quantity tracking) ───────────
    quantity_percent_complete: float | None = Field(
        default=None,
        description=(
            "Percentage of planned quantity installed for this cost code "
            "over the same date range. null when no quantity data is available."
        ),
    )
    quantity_completion: str | None = Field(
        default=None,
        description=(
            "'complete' when quantity_percent_complete >= 100, "
            "'not complete' otherwise. null when no quantity data is available."
        ),
    )

class BudgetVerifyResponse(BaseModel):
    period_start: str
    period_end: str
    total: int
    on_track: int
    over_risk: int
    items: list[BudgetItemRow]

class BudgetNotifyRequest(BaseModel):
    date:         str | None = Field(default=None)
    start_date:   str | None = Field(default=None)
    end_date:     str | None = Field(default=None)
    job_id:       str | None = Field(default=None)
    business_unit: str | None = Field(default=None)
    cost_code:    str | None = Field(default=None)
    recipients: list[EmailStr] = Field(..., min_length=1)
    comments:     str = Field(default="")

class BudgetNotifyResponse(BaseModel):
    period_start: str
    period_end: str
    recipients_count: int
    items_included: int
    message: str


# ── Helpers ───────────────────────────────────────────────────────────────────

def _validate_date(date_str: str, label: str) -> str:
    try:
        date_type.fromisoformat(date_str)
    except ValueError:
        raise HTTPException(status_code=422,
            detail=f"Invalid {label} \'{date_str}\'. Expected YYYY-MM-DD.")
    return date_str


def _resolve_period(date, start_date, end_date):
    if date:
        d = _validate_date(date, "date")
        return d, d
    if start_date or end_date:
        if not start_date or not end_date:
            raise HTTPException(status_code=422,
                detail="Both start_date and end_date are required for range queries.")
        sd = _validate_date(start_date, "start_date")
        ed = _validate_date(end_date, "end_date")
        if ed < sd:
            raise HTTPException(status_code=422, detail="end_date must be after start_date.")
        if (date_type.fromisoformat(ed) - date_type.fromisoformat(sd)).days > 90:
            raise HTTPException(status_code=422, detail="Date range cannot exceed 90 days.")
        return sd, ed
    raise HTTPException(status_code=422,
        detail="Either date or start_date+end_date is required.")


def _date_list(start_date: str, end_date: str) -> list[str]:
    dates, cur = [], datetime.fromisoformat(start_date)
    end = datetime.fromisoformat(end_date)
    while cur <= end:
        dates.append(cur.strftime("%Y-%m-%d"))
        cur += timedelta(days=1)
    return dates


def _compute_expected_budget(
    item: dict,
    qty_lookup: dict[tuple, dict],
) -> float:
    """
    Matches HCSS portal Expected All Cost (isCapExpected=true, which is the default):

        formula         = (installed_quantity / planned_quantity) * budgeted_all_cost
        expected_budget = min(formula, budgeted_all_cost)

    The cap prevents expected_budget from exceeding budgeted_all_cost when a cost
    code is >100% complete — identical to _compute_expected_for_cc() in the
    get_job_expected_all_cost.py script.

    T&M cost codes (is_tm=true) are not flagged in the budget DB rows, so they
    fall through to the quantity-based formula. If no qty data is available they
    return 0.0, consistent with the rest of the function.

    Both installed_quantity and planned_quantity come from the quantity tracking
    DB — the same values returned by the quantity/verify API:
      installed_quantity = SUM of costCode/progress["quantity"] for the period
      planned_quantity   = budgeted quantity from v2/costCodes

    Returns 0.0 when:
      - no quantity data found in qty_lookup for this cost code
      - planned_quantity is 0
      - installed_quantity is 0
    """
    jid  = item.get("job_id", "")
    ccid = item.get("cost_code_id", "")
    cc   = (item.get("cost_code") or "").strip()

    # Look up both planned and installed quantity from quantity tracking data
    qty_row = qty_lookup.get((jid, ccid)) or qty_lookup.get((jid, cc))
    if qty_row is None:
        return 0.0

    planned   = float(qty_row.get("planned_quantity")   or 0)
    installed = float(qty_row.get("installed_quantity") or 0)

    if planned <= 0 or installed <= 0:
        return 0.0

    budgeted_all_cost = float(item.get("budgeted_all_cost", 0))
    if budgeted_all_cost <= 0:
        return 0.0

    # Same as script: min((installed/planned)*budget, budget)  — isCapExpected=true
    formula = (installed / planned) * budgeted_all_cost
    return round(min(formula, budgeted_all_cost), 2)


def _calc_metrics(expected_budget: float, actual_cost: float) -> dict:
    """
    Compute all metrics from expected_budget and actual_cost.
    Used in both _aggregate_budget() and _to_budget_results().
    """
    if expected_budget > 0:
        utilization = round((actual_cost / expected_budget) * 100)
    else:
        utilization = None

    variance    = expected_budget - actual_cost
    loss_amount = round(max(0.0, actual_cost - expected_budget), 2)
    loss_pct    = (
        round((loss_amount / expected_budget) * 100, 2)
        if expected_budget > 0 else None
    )

    if expected_budget <= 0:
        status = "OVER_RISK" if actual_cost > 0 else "ON_TRACK"
    elif utilization < 75:
        status = "ON_TRACK"
    else:
        status = "OVER_RISK"

    return {
        "expected_budget":       expected_budget,
        "utilization_percentage": utilization,
        "variance":              variance,
        "loss_amount":           loss_amount,
        "loss_pct":              loss_pct,
        "status":                status,
    }


def _aggregate_budget(raw: list[dict], qty_lookup: dict[tuple, dict] | None = None) -> list[dict]:
    """
    Aggregate DB rows by (job_id, cost_code_id), summing costs across dates.

    Then computes expected_budget on the fly:
        expected_budget = (installed_qty / planned_qty) * budgeted_all_cost

    Zero when no quantity data is available — never falls back to budgeted_all_cost.

    All metrics (utilization_percentage, variance, loss_amount, loss_pct, status)
    are derived from expected_budget, not read from DB.
    """
    agg: dict[tuple, dict] = {}
    for item in raw:
        if not item.get("cost_code", "").strip():
            continue
        key = (item["job_id"], item["cost_code_id"])
        if key in agg:
            agg[key]["actual_cost"]      += item.get("actual_cost", 0)
            agg[key]["labor_cost"]       += item.get("labor_cost", 0)
            agg[key]["equipment_cost"]   += item.get("equipment_cost", 0)
            agg[key]["material_cost"]    += item.get("material_cost", 0)
            agg[key]["subcontract_cost"] += item.get("subcontract_cost", 0)
            agg[key]["trucking_cost"]    += item.get("trucking_cost", 0)
            agg[key]["labor_hours"]      += item.get("labor_hours", 0)
            agg[key]["quantity"]         += item.get("quantity", 0)
        else:
            agg[key] = dict(item)
            agg[key].setdefault("labor_cost", 0.0)
            agg[key].setdefault("equipment_cost", 0.0)
            agg[key].setdefault("material_cost", 0.0)
            agg[key].setdefault("subcontract_cost", 0.0)
            agg[key].setdefault("trucking_cost", 0.0)
            agg[key].setdefault("labor_hours", 0.0)
            agg[key].setdefault("quantity", 0.0)
            agg[key].setdefault("labor_budget", 0.0)
            agg[key].setdefault("equipment_budget", 0.0)
            agg[key].setdefault("material_budget", 0.0)
            agg[key].setdefault("subcontract_budget", 0.0)
            agg[key].setdefault("foremen", [])

    result = []
    for item in agg.values():
        # Compute expected_budget on the fly from qty data
        expected_budget = (
            _compute_expected_budget(item, qty_lookup)
            if qty_lookup is not None else 0.0
        )
        metrics = _calc_metrics(expected_budget, item["actual_cost"])
        item.update(metrics)
        result.append(item)
    return result


def _fetch_for_range(sd, ed, job_id=None, business_unit=None, cost_code=None, request_id="-"):
    """
    Fetch budget data from DB for a date range, aggregate, and compute all metrics.

    expected_budget = (installed_qty / planned_qty) * budgeted_all_cost
    is computed on the fly here using quantity tracking data for the same period.
    Zero when no quantity data is available.

    All metrics (utilization_percentage, variance, loss_amount, loss_pct, status)
    are derived from expected_budget — never read from the DB columns.
    """
    from api.db_services import get_or_fetch_budget_for_date
    dates = _date_list(sd, ed)
    raw: list[dict] = []
    db = SessionLocal()
    try:
        for d in dates:
            raw.extend(get_or_fetch_budget_for_date(db, d))
    finally:
        db.close()

    if not raw:
        raise HTTPException(status_code=404,
            detail=f"No budget data available for {sd} to {ed}.")

    # Apply filters before aggregation
    if job_id:
        raw = [r for r in raw if r.get("job_id") == job_id]
    if business_unit:
        raw = [r for r in raw if r.get("business_unit") == business_unit]
    if cost_code:
        raw = [r for r in raw if r.get("cost_code") == cost_code]

    # Fetch qty data for the same period — used to compute expected_budget on the fly
    qty_lookup = _fetch_quantity_lookup(sd, ed)

    aggregated = _aggregate_budget(raw, qty_lookup)
    logger.info("Budget fetch: %s to %s total=%d request_id=%s",
        sd, ed, len(aggregated), request_id)
    return aggregated, qty_lookup


def _to_budget_results(items: list[dict]) -> list[BudgetResult]:
    """Convert raw dicts to BudgetResult for calculate_summary_counts.
    expected_budget and all metrics are already computed in _aggregate_budget().
    """
    return [BudgetResult(
        cost_code_id=i["cost_code_id"], cost_code=i["cost_code"],
        cost_code_description=i["cost_code_description"],
        job_id=i["job_id"], job_name=i["job_name"],
        business_unit=i.get("business_unit") or "N/A",
        budgeted_all_cost=i.get("budgeted_all_cost", 0.0),
        actual_cost=i.get("actual_cost", 0.0),
    ) for i in items]


def _fetch_quantity_lookup(sd: str, ed: str) -> dict[tuple, dict]:
    """
    Fetch quantity tracking data for the same date range as the budget request
    and return a lookup dict keyed by (job_id, cost_code_id).

    Secondary keys are also registered under (job_id, cost_code) so that rows
    can be matched even when cost_code_ids differ between the two data sets.
    Falls back to an empty dict on any failure so the budget response is never
    blocked by a missing quantity fetch.
    """
    from api.db_services import get_or_fetch_quantity_for_period

    try:
        db = SessionLocal()
        try:
            raw = get_or_fetch_quantity_for_period(db, sd, ed)
        finally:
            db.close()
    except Exception as exc:
        logger.warning("Could not fetch quantity data for budget/verify: %s", exc)
        return {}

    lookup: dict[tuple, dict] = {}
    for q in raw:
        jid  = q.get("job_id", "")
        ccid = q.get("cost_code_id", "")
        cc   = (q.get("cost_code") or "").strip()
        desc = (q.get("description") or "").strip()

        # Primary key: exact (job_id, cost_code_id) match
        if jid and ccid:
            lookup[(jid, ccid)] = q

        # Secondary key: (job_id, cost_code string) — for cross-data-set matching
        if jid and cc:
            lookup.setdefault((jid, cc), q)

    return lookup


def _enrich_with_quantity(item: dict, qty_lookup: dict[tuple, dict]) -> tuple[float | None, str | None]:
    """
    Look up the matching quantity row for a budget item and return
    (quantity_percent_complete, quantity_completion).

    Matching priority:
      1. (job_id, cost_code_id)
      2. (job_id, cost_code)
    """
    jid  = item.get("job_id", "")
    ccid = item.get("cost_code_id", "")
    cc   = (item.get("cost_code") or "").strip()

    qty_row = (
        qty_lookup.get((jid, ccid))
        or qty_lookup.get((jid, cc))
    )

    if qty_row is None:
        return None, None

    planned   = float(qty_row.get("planned_quantity") or 0)
    installed = float(qty_row.get("installed_quantity") or 0)

    if planned <= 0:
        return None, None

    pct = round((installed / planned) * 100, 2)
    completion = "complete" if pct >= 100.0 else "not complete"
    return pct, completion


def _to_budget_item_row(i: dict, qty_lookup: dict[tuple, dict] | None = None) -> BudgetItemRow:
    """Map a raw dict (already aggregated with computed metrics) to BudgetItemRow."""
    qty_pct, qty_completion = (
        _enrich_with_quantity(i, qty_lookup) if qty_lookup is not None else (None, None)
    )
    return BudgetItemRow(
        cost_code_id=i["cost_code_id"],
        cost_code=i["cost_code"],
        cost_code_description=i["cost_code_description"],
        job_id=i["job_id"],
        job_name=i["job_name"],
        business_unit=i.get("business_unit") or "N/A",
        budgeted_all_cost=i.get("budgeted_all_cost", 0.0),
        expected_budget=i.get("expected_budget", 0.0),
        actual_cost=i.get("actual_cost", 0.0),
        utilization_percentage=i.get("utilization_percentage"),
        variance=i.get("variance", 0.0),
        loss_amount=i.get("loss_amount", 0.0),
        loss_pct=i.get("loss_pct"),
        status=i.get("status", "ON_TRACK"),
        labor_cost=i.get("labor_cost", 0.0),
        equipment_cost=i.get("equipment_cost", 0.0),
        material_cost=i.get("material_cost", 0.0),
        subcontract_cost=i.get("subcontract_cost", 0.0),
        trucking_cost=i.get("trucking_cost", 0.0),
        labor_hours=i.get("labor_hours", 0.0),
        quantity=i.get("quantity", 0.0),
        labor_budget=i.get("labor_budget", 0.0),
        equipment_budget=i.get("equipment_budget", 0.0),
        material_budget=i.get("material_budget", 0.0),
        subcontract_budget=i.get("subcontract_budget", 0.0),
        planned_quantity=i.get("planned_quantity", 0.0),
        unit=i.get("unit", "") or "",
        foremen=i.get("foremen", []),
        quantity_percent_complete=qty_pct,
        quantity_completion=qty_completion,
    )


# ── Route 1: Count ────────────────────────────────────────────────────────────

@router.get("/count", response_model=BudgetCountResponse,
    summary="Summary counts - On Track / Over Risk",
    responses={404: {"description": "No data"}, 502: {"model": ErrorResponse}})
def budget_count(
    date:          str | None = Query(default=None),
    start_date:    str | None = Query(default=None),
    end_date:      str | None = Query(default=None),
    job_id:        str | None = Query(default=None),
    business_unit: str | None = Query(default=None),
    cost_code:     str | None = Query(default=None),
) -> BudgetCountResponse:
    request_id = str(uuid.uuid4())
    sd, ed = _resolve_period(date, start_date, end_date)
    items, _  = _fetch_for_range(sd, ed, job_id, business_unit, cost_code, request_id)
    on_track  = sum(1 for i in items if i.get("status") == "ON_TRACK")
    over_risk = sum(1 for i in items if i.get("status") == "OVER_RISK")
    logger.info("Budget count: %s to %s total=%d", sd, ed, len(items))
    return BudgetCountResponse(period_start=sd, period_end=ed,
        total=len(items), on_track=on_track, over_risk=over_risk)


# ── Route 2: Verify ───────────────────────────────────────────────────────────

@router.get("/verify", response_model=BudgetVerifyResponse,
    summary="All budget items with detailed breakdown",
    responses={404: {"description": "No data"}, 502: {"model": ErrorResponse}})
def budget_verify(
    date:          str | None = Query(default=None),
    start_date:    str | None = Query(default=None),
    end_date:      str | None = Query(default=None),
    job_id:        str | None = Query(default=None),
    business_unit: str | None = Query(default=None),
    cost_code:     str | None = Query(default=None),
) -> BudgetVerifyResponse:
    request_id = str(uuid.uuid4())
    sd, ed  = _resolve_period(date, start_date, end_date)
    # qty_lookup is fetched inside _fetch_for_range and already used for expected_budget.
    # We reuse it here for quantity_percent_complete / quantity_completion enrichment.
    items, qty_lookup = _fetch_for_range(sd, ed, job_id, business_unit, cost_code, request_id)
    on_track  = sum(1 for i in items if i.get("status") == "ON_TRACK")
    over_risk = sum(1 for i in items if i.get("status") == "OVER_RISK")
    logger.info(
        "Budget verify: %s to %s returning %d items, qty_lookup size=%d",
        sd, ed, len(items), len(qty_lookup),
    )
    return BudgetVerifyResponse(period_start=sd, period_end=ed,
        total=len(items), on_track=on_track, over_risk=over_risk,
        items=[_to_budget_item_row(i, qty_lookup) for i in items])


# ── Route 3: Notify ───────────────────────────────────────────────────────────

@router.post("/notify", response_model=BudgetNotifyResponse,
    summary="Send budget report email",
    responses={422: {"model": ErrorResponse}, 502: {"model": ErrorResponse}})
def budget_notify(payload: BudgetNotifyRequest) -> BudgetNotifyResponse:
    request_id = str(uuid.uuid4())
    sd, ed  = _resolve_period(payload.date, payload.start_date, payload.end_date)
    items, qty_lookup = _fetch_for_range(sd, ed, payload.job_id, payload.business_unit,
                                         payload.cost_code, request_id)
    subject, html_body = build_budget_email_body(period_start=sd, period_end=ed,
        items=[_to_budget_item_row(i, qty_lookup) for i in items], comments=payload.comments)
    recipient_list = [str(r) for r in payload.recipients]
    try:
        send_budget_email(subject=subject, html_body=html_body, recipients=recipient_list)
    except ValueError as exc:
        raise HTTPException(status_code=502, detail=f"Email config error: {exc}") from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Email send failed: {exc}") from exc
    logger.info("Budget notify sent: %d items to %d recipients", len(items), len(recipient_list))
    return BudgetNotifyResponse(period_start=sd, period_end=ed,
        recipients_count=len(recipient_list), items_included=len(items),
        message=f"Budget report sent to {len(recipient_list)} recipient(s) with {len(items)} cost code(s).")