"""
api/routes/postmortem_routes.py
────────────────────────────────
Postmortem Analysis API routes — Phase 10.

Endpoints:
  GET  /api/postmortem/list          — all projects with summary KPI table
  GET  /api/postmortem/detail        — full postmortem for a single project
  POST /api/postmortem/notify        — email a postmortem report on demand

All data is read from existing DB tables (budget, quantity, projection,
timelog).  No new HCSS API calls are made.

Follows the same structure as api/routes/budget.py and
api/routes/projection_routes.py.
"""
from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import EmailStr
from sqlalchemy.orm import Session

from api.database import get_db
from api.schemas import ErrorResponse
from postmortem.analyzer import build_postmortem_for_job, get_all_job_ids
from postmortem.schemas import (
    CostCodeSummarySchema,
    DeferredSectionSchema,
    MissedCostItemSchema,
    PostmortemDetailResponse,
    PostmortemListResponse,
    PostmortemNotifyRequest,
    PostmortemNotifyResponse,
    PostmortemProjectRow,
    ProjectionSummarySchema,
    QuantityCodeSummarySchema,
    TimelogSummarySchema,
)
from postmortem.email_template import build_postmortem_email_body
from notifications.budget_email_sender import send_budget_email

router = APIRouter(prefix="/postmortem", tags=["Postmortem Analysis"])
logger = logging.getLogger(__name__)


# ── Deferred section defaults ─────────────────────────────────────────────────

_DEFERRED_CHANGE_ORDERS = DeferredSectionSchema(
    available=False,
    depends_on="Billing Draft",
    message=(
        "Change order data will be available once the Billing Draft "
        "module is complete."
    ),
)

_DEFERRED_SCHEDULE = DeferredSectionSchema(
    available=False,
    depends_on="Schedule Monitoring",
    message=(
        "Schedule performance data will be available once the Schedule "
        "Monitoring module is complete."
    ),
)


# ── Converters ────────────────────────────────────────────────────────────────

def _to_detail_response(result) -> PostmortemDetailResponse:
    """Convert a PostmortemResult dataclass to the API response schema."""
    return PostmortemDetailResponse(
        job_id=result.job_id,
        job_code=result.job_code,
        job_name=result.job_name,
        business_unit=result.business_unit,
        overall_risk=result.overall_risk,
        total_expected_budget=result.total_expected_budget,
        total_actual_cost=result.total_actual_cost,
        total_variance=result.total_variance,
        total_variance_pct=result.total_variance_pct,
        total_loss_amount=result.total_loss_amount,
        total_loss_pct=result.total_loss_pct,
        over_risk_cost_codes=result.over_risk_cost_codes,
        near_completion_qty_codes=result.near_completion_qty_codes,
        over_risk_qty_codes=result.over_risk_qty_codes,
        missed_cost_count=result.missed_cost_count,
        estimated_missed_cost_total=result.estimated_missed_cost_total,
        cost_codes=[
            CostCodeSummarySchema(
                cost_code_id=c.cost_code_id,
                cost_code=c.cost_code,
                cost_code_description=c.cost_code_description,
                expected_budget=c.expected_budget,
                actual_cost=c.actual_cost,
                variance=c.variance,
                utilization_pct=c.utilization_pct,
                loss_amount=c.loss_amount,
                loss_pct=c.loss_pct,
                status=c.status,
            )
            for c in result.cost_codes
        ],
        quantities=[
            QuantityCodeSummarySchema(
                cost_code_id=q.cost_code_id,
                cost_code=q.cost_code,
                description=q.description,
                unit=q.unit,
                cost_type=q.cost_type,
                planned_quantity=q.planned_quantity,
                installed_quantity=q.installed_quantity,
                remaining_quantity=q.remaining_quantity,
                percent_complete=q.percent_complete,
                status=q.status,
                alert=q.alert,
            )
            for q in result.quantities
        ],
        missed_costs=[
            MissedCostItemSchema(
                cost_code_id=m.cost_code_id,
                cost_code=m.cost_code,
                description=m.description,
                qty_percent_complete=m.qty_percent_complete,
                cost_utilization_pct=m.cost_utilization_pct,
                expected_budget=m.expected_budget,
                actual_cost=m.actual_cost,
                estimated_missed_value=m.estimated_missed_value,
                source=m.source,
                detail=m.detail,
            )
            for m in result.missed_costs
        ],
        timelog=(
            TimelogSummarySchema(
                total_timecards=result.timelog.total_timecards,
                approved=result.timelog.approved,
                flagged=result.timelog.flagged,
                rejected=result.timelog.rejected,
                rejection_reasons=result.timelog.rejection_reasons,
                flag_reasons=result.timelog.flag_reasons,
                quality_score_pct=result.timelog.quality_score_pct,
            )
            if result.timelog else None
        ),
        projection=(
            ProjectionSummarySchema(
                tracking_month=result.projection.tracking_month,
                original_budget=result.projection.original_budget,
                actual_cost_to_date=result.projection.actual_cost_to_date,
                projected_final_cost=result.projection.projected_final_cost,
                cost_variance=result.projection.cost_variance,
                percent_complete=result.projection.percent_complete,
                original_contract_value=result.projection.original_contract_value,
                approved_change_orders=result.projection.approved_change_orders,
                revised_contract_value=result.projection.revised_contract_value,
                billed_to_date=result.projection.billed_to_date,
                projected_final_billing=result.projection.projected_final_billing,
                billing_variance=result.projection.billing_variance,
                estimated_completion_month=result.projection.estimated_completion_month,
                status=result.projection.status,
                alert=result.projection.alert,
                discrepancy_flag=result.projection.discrepancy_flag,
            )
            if result.projection else None
        ),
        change_orders=_DEFERRED_CHANGE_ORDERS,
        schedule_performance=_DEFERRED_SCHEDULE,
    )


def _to_project_row(result) -> PostmortemProjectRow:
    """Convert a PostmortemResult to the summary table row schema."""
    return PostmortemProjectRow(
        job_id=result.job_id,
        job_code=result.job_code,
        job_name=result.job_name,
        business_unit=result.business_unit,
        overall_risk=result.overall_risk,
        total_expected_budget=result.total_expected_budget,
        total_actual_cost=result.total_actual_cost,
        total_variance=result.total_variance,
        total_variance_pct=result.total_variance_pct,
        total_loss_amount=result.total_loss_amount,
        total_loss_pct=result.total_loss_pct,
        over_risk_cost_codes=result.over_risk_cost_codes,
        near_completion_qty_codes=result.near_completion_qty_codes,
        over_risk_qty_codes=result.over_risk_qty_codes,
        missed_cost_count=result.missed_cost_count,
        estimated_missed_cost_total=result.estimated_missed_cost_total,
        timelog_quality_score_pct=(
            result.timelog.quality_score_pct if result.timelog else None
        ),
        projection_status=(
            result.projection.status if result.projection else None
        ),
        projection_percent_complete=(
            result.projection.percent_complete if result.projection else None
        ),
    )


# ── Route 1: List ─────────────────────────────────────────────────────────────

@router.get(
    "/list",
    response_model=PostmortemListResponse,
    summary="All projects — postmortem KPI summary table",
    description=(
        "Returns a summary row for every project that has data in the system.\n\n"
        "Each row contains the key postmortem KPIs:\n"
        "- Overall risk (ON_TRACK / AT_RISK / OVER_BUDGET)\n"
        "- Cost variance and loss\n"
        "- Quantity completion status\n"
        "- Missed cost count and estimated value\n"
        "- Time log quality score\n"
        "- Latest projection status\n\n"
        "Use `GET /api/postmortem/detail?job_id=xxx` to drill into a specific project.\n\n"
        "**Filters:** `business_unit`, `overall_risk`, `search` (partial match on job code or name)."
    ),
    responses={
        404: {"description": "No project data found in the system"},
        502: {"model": ErrorResponse},
    },
)
def postmortem_list(
    business_unit: str | None = Query(
        default=None,
        description="Filter by business unit name",
    ),
    overall_risk: str | None = Query(
        default=None,
        description="Filter by risk level: ON_TRACK | AT_RISK | OVER_BUDGET",
    ),
    search: str | None = Query(
        default=None,
        description="Partial match on job code or job name (case-insensitive)",
    ),
    db: Session = Depends(get_db),
) -> PostmortemListResponse:
    request_id = str(uuid.uuid4())
    logger.info(
        "Postmortem list: bu=%s risk=%s search=%s [%s]",
        business_unit, overall_risk, search, request_id,
    )

    # Discover all known job IDs
    job_entries = get_all_job_ids(db)
    if not job_entries:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                "No project data found. Ensure the budget, quantity, or projection "
                "cron jobs have run at least once."
            ),
        )

    # Apply pre-filters on identity fields before running the expensive aggregation
    if business_unit:
        job_entries = [
            j for j in job_entries
            if (j.get("business_unit") or "").lower() == business_unit.lower()
        ]
    if search:
        term = search.lower()
        job_entries = [
            j for j in job_entries
            if term in (j.get("job_code") or "").lower()
            or term in (j.get("job_name") or "").lower()
        ]

    if not job_entries:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No projects match the specified filters.",
        )

    # Build postmortem for each job
    rows: list[PostmortemProjectRow] = []
    for entry in job_entries:
        jid = entry["job_id"]
        try:
            result = build_postmortem_for_job(db, jid)
            if result is None:
                continue
            rows.append(_to_project_row(result))
        except Exception as exc:
            logger.error(
                "Failed to build postmortem for job_id=%s: %s [%s]",
                jid, exc, request_id,
            )
            continue

    if not rows:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No postmortem data could be assembled for the matching projects.",
        )

    # Apply post-filter on overall_risk (computed during aggregation)
    if overall_risk:
        rows = [r for r in rows if r.overall_risk == overall_risk.upper()]
        if not rows:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"No projects with overall_risk={overall_risk.upper()}.",
            )

    # Summary counts
    on_track    = sum(1 for r in rows if r.overall_risk == "ON_TRACK")
    at_risk     = sum(1 for r in rows if r.overall_risk == "AT_RISK")
    over_budget = sum(1 for r in rows if r.overall_risk == "OVER_BUDGET")
    total_missed = round(sum(r.estimated_missed_cost_total for r in rows), 2)

    logger.info(
        "Postmortem list: total=%d on_track=%d at_risk=%d over_budget=%d [%s]",
        len(rows), on_track, at_risk, over_budget, request_id,
    )

    return PostmortemListResponse(
        total_projects=len(rows),
        on_track=on_track,
        at_risk=at_risk,
        over_budget=over_budget,
        total_missed_cost=total_missed,
        projects=rows,
    )


# ── Route 2: Detail ───────────────────────────────────────────────────────────

@router.get(
    "/detail",
    response_model=PostmortemDetailResponse,
    summary="Full postmortem for a single project",
    description=(
        "Returns the complete postmortem analysis for one project, including:\n\n"
        "- **Cost vs Budget** — per cost code breakdown with variance and loss\n"
        "- **Quantity Completion** — planned vs installed per cost code\n"
        "- **Missed Costs** — cross-referenced signals of unrecorded costs\n"
        "- **Time Log Quality** — approved / flagged / rejected timecards\n"
        "- **Projection Summary** — latest EAC, billing position, % complete\n"
        "- **Change Orders** — stub (available after Billing Draft module)\n"
        "- **Schedule Performance** — stub (available after Schedule Monitoring module)\n\n"
        "Pass the `job_id` (HCSS job UUID) from the `/list` endpoint."
    ),
    responses={
        404: {"description": "No data found for the specified job_id"},
        422: {"model": ErrorResponse, "description": "Missing or invalid job_id"},
        502: {"model": ErrorResponse},
    },
)
def postmortem_detail(
    job_id: str = Query(
        ...,
        description="HCSS job UUID — obtain from GET /api/postmortem/list",
    ),
    db: Session = Depends(get_db),
) -> PostmortemDetailResponse:
    request_id = str(uuid.uuid4())
    logger.info("Postmortem detail: job_id=%s [%s]", job_id, request_id)

    if not job_id or not job_id.strip():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="job_id is required.",
        )

    try:
        result = build_postmortem_for_job(db, job_id.strip())
    except Exception as exc:
        logger.exception(
            "Unexpected error building postmortem for job_id=%s [%s]",
            job_id, request_id,
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to build postmortem: {exc}",
        ) from exc

    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                f"No data found for job_id={job_id}. "
                "Ensure the cron jobs have run and this job has activity in the system."
            ),
        )

    logger.info(
        "Postmortem detail: job_id=%s job_code=%s risk=%s [%s]",
        job_id, result.job_code, result.overall_risk, request_id,
    )
    return _to_detail_response(result)


# ── Route 3: Notify ───────────────────────────────────────────────────────────

@router.post(
    "/notify",
    response_model=PostmortemNotifyResponse,
    summary="Email a postmortem report for a project",
    description=(
        "Builds the full postmortem for the specified project and sends an HTML "
        "report email to the provided recipients.\n\n"
        "The email includes:\n"
        "- KPI summary cards\n"
        "- Projection snapshot\n"
        "- Cost vs budget table\n"
        "- Missed cost table\n"
        "- Time log quality block\n"
        "- Optional user comments\n\n"
        "Reuses the existing SMTP sender from the Budget Tracking module."
    ),
    responses={
        404: {"description": "No data found for the specified job_id"},
        422: {"model": ErrorResponse},
        502: {"model": ErrorResponse},
    },
)
def postmortem_notify(
    payload: PostmortemNotifyRequest,
    db: Session = Depends(get_db),
) -> PostmortemNotifyResponse:
    request_id = str(uuid.uuid4())
    logger.info(
        "Postmortem notify: job_id=%s recipients=%d [%s]",
        payload.job_id, len(payload.recipients), request_id,
    )

    # Build the postmortem
    try:
        result = build_postmortem_for_job(db, payload.job_id.strip())
    except Exception as exc:
        logger.exception(
            "Failed to build postmortem for notify: job_id=%s [%s]",
            payload.job_id, request_id,
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to build postmortem: {exc}",
        ) from exc

    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                f"No data found for job_id={payload.job_id}. "
                "Ensure the cron jobs have run and this job has activity in the system."
            ),
        )

    detail = _to_detail_response(result)

    # Build email
    subject, html_body = build_postmortem_email_body(
        detail=detail,
        comments=payload.comments,
        triggered_by="Manual",
    )

    # Send via existing SMTP sender
    recipient_list = [str(r) for r in payload.recipients]
    try:
        send_budget_email(
            subject=subject,
            html_body=html_body,
            recipients=recipient_list,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Email configuration error: {exc}",
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Email send failed: {exc}",
        ) from exc

    logger.info(
        "Postmortem notify sent: job_id=%s job_code=%s recipients=%d [%s]",
        payload.job_id, result.job_code, len(recipient_list), request_id,
    )
    return PostmortemNotifyResponse(
        job_id=payload.job_id,
        job_code=result.job_code,
        recipients_count=len(recipient_list),
        message=(
            f"Postmortem report for {result.job_code} — {result.job_name} "
            f"sent to {len(recipient_list)} recipient(s)."
        ),
    )
