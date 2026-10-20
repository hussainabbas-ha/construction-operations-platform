"""
api/routes/schedule_routes.py
───────────────────────────────
Schedule Monitoring API routes.

Same 3-endpoint pattern as every other module:
  GET  /api/schedule/count   — summary counts for a week or date range
  GET  /api/schedule/verify  — full detail rows (quantities, costs, labor, time)
  POST /api/schedule/notify  — send email on demand

Data shape mirrors the 4 comparison columns:
  - quantities   : planned (baseline — needs GDrive) / actual (DB) / status
  - cost/budget  : planned (baseline — needs GDrive) / actual (DB) / status
  - labor hours  : planned (baseline — needs GDrive) / actual (DB) / status
  - time/dates   : planned (baseline — needs GDrive) / actual (HCSS job record) / status

Fields marked "NEEDS_BASELINE" will be null until the 6-week GDrive schedule is connected.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, EmailStr, Field

from api.database import SessionLocal
from api.schemas import ErrorResponse

router = APIRouter(prefix="/schedule", tags=["Schedule Monitoring"])
logger = logging.getLogger(__name__)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _last_friday() -> str:
    today = date.today()
    days_since_friday = (today.weekday() - 4) % 7
    return (today - timedelta(days=days_since_friday)).strftime("%Y-%m-%d")


def _validate_date(d: str, label: str) -> str:
    try:
        date.fromisoformat(d)
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail=f"Invalid {label} '{d}'. Expected YYYY-MM-DD.",
        )
    return d


def _resolve_period(
    start_date: str | None,
    end_date: str | None,
) -> tuple[str, str]:
    """Default to last full week (Mon–Fri) if no dates given."""
    if start_date and end_date:
        return (
            _validate_date(start_date, "start_date"),
            _validate_date(end_date, "end_date"),
        )
    friday = _last_friday()
    monday = (date.fromisoformat(friday) - timedelta(days=4)).strftime("%Y-%m-%d")
    return monday, friday


# ── Schemas ───────────────────────────────────────────────────────────────────

class ScheduleCountResponse(BaseModel):
    """Summary counts — mirrors BudgetCountResponse / QuantityCountResponse."""
    period_start: str
    period_end: str
    total_jobs: int
    on_track: int
    warning: int
    critical: int
    total_alerts: int


class CostCodeScheduleRow(BaseModel):
    """
    One row per cost code — the 4 comparison columns.
    planned_* fields are null until GDrive baseline is connected.
    """
    cost_code_id: str
    cost_code: str
    description: str
    job_id: str
    job_code: str
    job_name: str
    unit: str
    cost_type: str                        # self_perform | subcontractor

    # ── Quantities ────────────────────────────────────────────────────
    planned_quantity: float | None = Field(
        default=None,
        description="NEEDS_BASELINE: from 6-week GDrive schedule"
    )
    actual_quantity: float
    quantity_pct: float                   # actual / planned * 100 (0 if no baseline)
    quantity_status: str                  # ON_TRACK | WARNING | CRITICAL | NO_BASELINE

    # ── Cost / Budget ─────────────────────────────────────────────────
    planned_cost: float | None = Field(
        default=None,
        description="Budget from HCSS cost codes (available now)"
    )
    actual_cost: float
    cost_pct: float                       # actual / planned * 100
    cost_status: str                      # ON_TRACK | WARNING | CRITICAL

    # ── Labor Hours ───────────────────────────────────────────────────
    planned_labor_hours: float | None = Field(
        default=None,
        description="NEEDS_BASELINE: from 6-week GDrive schedule"
    )
    actual_labor_hours: float
    labor_hours_pct: float                # actual / planned * 100 (0 if no baseline)
    labor_status: str                     # ON_TRACK | WARNING | CRITICAL | NO_BASELINE

    # ── Flags ─────────────────────────────────────────────────────────
    has_activity: bool
    seasonal_risk: str | None = None      # asphalt | concrete | None
    alerts: list[str]                     # human-readable alert messages for this row


class JobScheduleRow(BaseModel):
    """One row per job with rolled-up totals."""
    job_id: str
    job_code: str
    job_name: str
    business_unit: str

    # ── Time / Dates ──────────────────────────────────────────────────
    planned_start_date: str | None = Field(
        default=None,
        description="NEEDS_BASELINE: from 6-week GDrive schedule"
    )
    planned_end_date: str | None = Field(
        default=None,
        description="NEEDS_BASELINE: from 6-week GDrive schedule"
    )
    actual_start_date: str | None         # from HCSS job record
    estimated_end_date: str | None        # from HCSS job record
    last_timecard_date: str | None        # most recent activity
    days_since_activity: int
    time_status: str                      # ON_TRACK | WARNING | CRITICAL | NO_BASELINE

    # ── Rolled-up totals ──────────────────────────────────────────────
    total_planned_cost: float | None      # sum of HCSS budgets (available)
    total_actual_cost: float
    cost_pct: float
    total_actual_labor_hours: float

    total_planned_quantity: float | None  # NEEDS_BASELINE for weekly planned
    total_actual_quantity: float
    quantity_pct: float

    # ── Status ────────────────────────────────────────────────────────
    status: str = Field(..., description="ON_TRACK | WARNING | CRITICAL")
    critical_alert_count: int
    warning_alert_count: int

    # ── Cost code detail ──────────────────────────────────────────────
    cost_codes: list[CostCodeScheduleRow]


class ScheduleVerifyResponse(BaseModel):
    """Full detail — mirrors BudgetVerifyResponse."""
    period_start: str
    period_end: str
    total_jobs: int
    on_track: int
    warning: int
    critical: int
    total_alerts: int
    baseline_connected: bool = Field(
        default=False,
        description="False until GDrive 6-week schedule is connected. "
                    "When False, planned_* fields are null and *_status "
                    "for quantities/labor shows NO_BASELINE."
    )
    jobs: list[JobScheduleRow]


class ScheduleNotifyRequest(BaseModel):
    start_date: str | None = Field(default=None, description="YYYY-MM-DD")
    end_date: str | None = Field(default=None, description="YYYY-MM-DD")
    recipients: list[EmailStr] = Field(..., min_length=1)
    comments: str = Field(default="")


class ScheduleNotifyResponse(BaseModel):
    period_start: str
    period_end: str
    recipients_count: int
    jobs_included: int
    message: str


# ── Shared data fetch + build ─────────────────────────────────────────────────

def _fetch_and_build(start: str, end: str) -> list[JobScheduleRow]:
    """
    Pull data from DB + HCSS, run alert rules, return list of JobScheduleRow.
    Called by both /count and /verify.
    """
    from schedule_monitoring.engine import run_schedule_monitoring
    from schedule_monitoring.data_gatherer import JobSnapshot, CostCodeSnapshot

    db = SessionLocal()
    try:
        report = run_schedule_monitoring(start_date=start, end_date=end, db=db)
    finally:
        db.close()

    # Re-gather snapshots to get the cost-code level detail for verify
    # (report has job summaries but not the full CostCodeSnapshot list)
    from schedule_monitoring.data_gatherer import ScheduleDataGatherer
    db2 = SessionLocal()
    try:
        gatherer = ScheduleDataGatherer(db=db2)
        snapshots = gatherer.gather_range(start, end)
    finally:
        db2.close()

    # Build alert lookup from report
    alert_map: dict[str, list[str]] = {}          # job_id → messages
    cc_alert_map: dict[tuple[str, str], list[str]] = {}  # (job_id, cc_id) → messages
    for a in report.alerts:
        alert_map.setdefault(a.job_id, []).append(a.message)
        if a.cost_code_id:
            key = (a.job_id, a.cost_code_id)
            cc_alert_map.setdefault(key, []).append(a.message)

    today = date.today()
    job_rows: list[JobScheduleRow] = []

    for snap in snapshots:
        summary = next(
            (j for j in report.job_summaries if j["job_id"] == snap.job_id), {}
        )
        status = summary.get("status", "ON_TRACK")

        # ── Time status ───────────────────────────────────────────────
        time_status = "NO_BASELINE"
        if snap.estimated_end_date:
            try:
                est = date.fromisoformat(snap.estimated_end_date)
                overrun = (today - est).days
                if overrun > 0:
                    time_status = "CRITICAL"
                elif overrun >= -14:
                    time_status = "WARNING"
                else:
                    time_status = "ON_TRACK"
            except ValueError:
                time_status = "NO_BASELINE"

        # ── Cost code rows ────────────────────────────────────────────
        cc_rows: list[CostCodeScheduleRow] = []
        for cc in snap.cost_codes:
            # Quantity status — no baseline for weekly planned qty
            if cc.quantity_pct >= 100:
                qty_status = "CRITICAL"
            elif cc.quantity_pct >= 75:
                qty_status = "WARNING"
            elif cc.planned_quantity > 0:
                qty_status = "ON_TRACK"
            else:
                qty_status = "NO_BASELINE"

            # Cost status — budget IS available from HCSS
            if cc.cost_pct >= 115:
                cost_status = "CRITICAL"
            elif cc.cost_pct >= 75:
                cost_status = "WARNING"
            else:
                cost_status = "ON_TRACK"

            # Labor status — no baseline for planned hours
            labor_status = "NO_BASELINE"

            cc_rows.append(CostCodeScheduleRow(
                cost_code_id=cc.cost_code_id,
                cost_code=cc.cost_code,
                description=cc.description,
                job_id=cc.job_id,
                job_code=cc.job_code,
                job_name=snap.job_name,
                unit=cc.unit,
                cost_type=cc.cost_type,
                # Quantities
                planned_quantity=cc.planned_quantity if cc.planned_quantity > 0 else None,
                actual_quantity=cc.installed_quantity,
                quantity_pct=cc.quantity_pct,
                quantity_status=qty_status,
                # Cost
                planned_cost=cc.budget_cost if cc.budget_cost > 0 else None,
                actual_cost=cc.actual_cost,
                cost_pct=cc.cost_pct,
                cost_status=cost_status,
                # Labor
                planned_labor_hours=None,   # NEEDS_BASELINE
                actual_labor_hours=cc.labor_hours,
                labor_hours_pct=0.0,        # NEEDS_BASELINE
                labor_status=labor_status,
                # Flags
                has_activity=cc.has_activity,
                seasonal_risk=cc.seasonal_risk,
                alerts=cc_alert_map.get((cc.job_id, cc.cost_code_id), []),
            ))

        job_rows.append(JobScheduleRow(
            job_id=snap.job_id,
            job_code=snap.job_code,
            job_name=snap.job_name,
            business_unit=snap.business_unit,
            # Time
            planned_start_date=None,        # NEEDS_BASELINE
            planned_end_date=None,          # NEEDS_BASELINE
            actual_start_date=snap.start_date,
            estimated_end_date=snap.estimated_end_date,
            last_timecard_date=snap.last_timecard_date,
            days_since_activity=snap.days_since_activity,
            time_status=time_status,
            # Totals
            total_planned_cost=snap.total_budget_cost if snap.total_budget_cost > 0 else None,
            total_actual_cost=snap.total_actual_cost,
            cost_pct=snap.cost_pct,
            total_actual_labor_hours=snap.total_labor_hours,
            total_planned_quantity=snap.total_planned_quantity if snap.total_planned_quantity > 0 else None,
            total_actual_quantity=snap.total_installed_quantity,
            quantity_pct=snap.quantity_pct,
            # Status
            status=status,
            critical_alert_count=summary.get("critical_alerts", 0),
            warning_alert_count=summary.get("warning_alerts", 0),
            cost_codes=cc_rows,
        ))

    return sorted(
        job_rows,
        key=lambda j: (0 if j.status == "CRITICAL" else 1 if j.status == "WARNING" else 2),
    )


# ── Route 1: Count ────────────────────────────────────────────────────────────

@router.get(
    "/count",
    response_model=ScheduleCountResponse,
    summary="Schedule monitoring summary counts",
    description=(
        "Returns job counts by status for the requested period.\n\n"
        "Defaults to last full Mon–Fri week if no dates given."
    ),
    responses={404: {"description": "No data"}, 502: {"model": ErrorResponse}},
)
def schedule_count(
    start_date: str | None = Query(default=None, description="YYYY-MM-DD"),
    end_date: str | None = Query(default=None, description="YYYY-MM-DD"),
) -> ScheduleCountResponse:
    start, end = _resolve_period(start_date, end_date)

    try:
        jobs = _fetch_and_build(start, end)
    except Exception as exc:
        logger.error("Schedule count failed: %s", exc, exc_info=True)
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    if not jobs:
        raise HTTPException(
            status_code=404,
            detail=f"No schedule data available for {start} to {end}.",
        )

    on_track  = sum(1 for j in jobs if j.status == "ON_TRACK")
    warning   = sum(1 for j in jobs if j.status == "WARNING")
    critical  = sum(1 for j in jobs if j.status == "CRITICAL")
    total_alerts = sum(j.critical_alert_count + j.warning_alert_count for j in jobs)

    logger.info(
        "Schedule count: %s→%s jobs=%d critical=%d warning=%d on_track=%d",
        start, end, len(jobs), critical, warning, on_track,
    )
    return ScheduleCountResponse(
        period_start=start,
        period_end=end,
        total_jobs=len(jobs),
        on_track=on_track,
        warning=warning,
        critical=critical,
        total_alerts=total_alerts,
    )


# ── Route 2: Verify ───────────────────────────────────────────────────────────

@router.get(
    "/verify",
    response_model=ScheduleVerifyResponse,
    summary="Full schedule detail — quantities, costs, labor, time per job and cost code",
    description=(
        "Returns all jobs with their 4 comparison columns per cost code:\n\n"
        "- **quantities**: planned (needs baseline) / actual / status\n"
        "- **cost/budget**: planned (HCSS budget, available now) / actual / status\n"
        "- **labor hours**: planned (needs baseline) / actual / status\n"
        "- **time/dates**: planned (needs baseline) / actual (HCSS) / status\n\n"
        "Fields marked `NEEDS_BASELINE` will be null until the GDrive 6-week schedule is connected.\n\n"
        "Filter by `status` (ON_TRACK | WARNING | CRITICAL) or `job_id`."
    ),
    responses={404: {"description": "No data"}, 502: {"model": ErrorResponse}},
)
def schedule_verify(
    start_date: str | None = Query(default=None, description="YYYY-MM-DD"),
    end_date: str | None = Query(default=None, description="YYYY-MM-DD"),
    status: str | None = Query(
        default=None,
        description="Filter by job status: ON_TRACK | WARNING | CRITICAL",
    ),
    job_id: str | None = Query(default=None, description="Filter to a single job UUID"),
) -> ScheduleVerifyResponse:
    start, end = _resolve_period(start_date, end_date)

    try:
        jobs = _fetch_and_build(start, end)
    except Exception as exc:
        logger.error("Schedule verify failed: %s", exc, exc_info=True)
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    if not jobs:
        raise HTTPException(
            status_code=404,
            detail=f"No schedule data available for {start} to {end}.",
        )

    if status:
        jobs = [j for j in jobs if j.status == status.upper()]
    if job_id:
        jobs = [j for j in jobs if j.job_id == job_id]

    on_track  = sum(1 for j in jobs if j.status == "ON_TRACK")
    warning   = sum(1 for j in jobs if j.status == "WARNING")
    critical  = sum(1 for j in jobs if j.status == "CRITICAL")
    total_alerts = sum(j.critical_alert_count + j.warning_alert_count for j in jobs)

    logger.info(
        "Schedule verify: %s→%s returning %d jobs",
        start, end, len(jobs),
    )
    return ScheduleVerifyResponse(
        period_start=start,
        period_end=end,
        total_jobs=len(jobs),
        on_track=on_track,
        warning=warning,
        critical=critical,
        total_alerts=total_alerts,
        baseline_connected=False,
        jobs=jobs,
    )


# ── Route 3: Notify ───────────────────────────────────────────────────────────

@router.post(
    "/notify",
    response_model=ScheduleNotifyResponse,
    summary="Send schedule monitoring report email on demand",
    description=(
        "Sends an HTML schedule report to the specified recipients.\n\n"
        "Defaults to last full Mon–Fri week."
    ),
    responses={422: {"model": ErrorResponse}, 502: {"model": ErrorResponse}},
)
def schedule_notify(payload: ScheduleNotifyRequest) -> ScheduleNotifyResponse:
    start, end = _resolve_period(payload.start_date, payload.end_date)

    try:
        jobs = _fetch_and_build(start, end)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    if not jobs:
        raise HTTPException(
            status_code=404,
            detail=f"No schedule data available for {start} to {end}.",
        )

    # Build report object for email template
    from schedule_monitoring.engine import run_schedule_monitoring
    db = SessionLocal()
    try:
        report = run_schedule_monitoring(start_date=start, end_date=end, db=db)
    finally:
        db.close()

    from schedule_monitoring.email_template import build_schedule_email
    subject, html_body = build_schedule_email(report, comments=payload.comments)

    recipient_list = [str(r) for r in payload.recipients]
    try:
        from notifications.budget_email_sender import send_budget_email
        send_budget_email(subject=subject, html_body=html_body, recipients=recipient_list)
    except ValueError as exc:
        raise HTTPException(status_code=502, detail=f"Email config error: {exc}") from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Email send failed: {exc}") from exc

    logger.info(
        "Schedule notify sent: %s→%s jobs=%d recipients=%d",
        start, end, len(jobs), len(recipient_list),
    )
    return ScheduleNotifyResponse(
        period_start=start,
        period_end=end,
        recipients_count=len(recipient_list),
        jobs_included=len(jobs),
        message=(
            f"Schedule report for {start} → {end} sent to "
            f"{len(recipient_list)} recipient(s) with {len(jobs)} job(s)."
        ),
    )
