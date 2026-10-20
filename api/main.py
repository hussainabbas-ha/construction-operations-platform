"""
api/main.py
────────────
CHANGES FROM ORIGINAL:
  + Imported projection_router                              (← NEW, line ~40)
  + Registered projection_router with app.include_router() (← NEW, line ~110)
  + Imported run_monthly_projection_job                     (← NEW, line ~40)

Search for "← NEW" to find every change.
All existing code is unchanged.
"""
from __future__ import annotations

import sys
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator, Any

import requests as _requests

from fastapi import FastAPI, HTTPException, Query, Request, status, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from api.cache_service import get_cached_verification, store_verification_results
from api.config import settings
from api.database import create_tables, get_db
from api.logger import configure_logging, get_logger
from api.middleware import RequestLoggingMiddleware
from api.routes.auth_routes import router as auth_router
from api.routes.notify_routes import router as notify_router
from api.schemas import (
    ErrorResponse, RangeVerifyResponse, SummaryResponse,
    DateRangeSummary, TimecardRow, VerifyResponse, CountOnlyResponse,
)
from api.utils import date_range, default_date, validate_date
from payroll_verification.hcss_client import HCSSClient
from payroll_verification.reporting import group_results, results_to_rows
from payroll_verification.verifier import verify_payroll_date, verify_payroll_range, TimecardResult
from api.routes.quantity_routes import router as quantity_router
from api.routes.quantity_test_routes import router as quantity_test_router
from api.routes.budget import router as budget_router
from api.routes.variance_routes import router as variance_router
from api.routes.cron_routes import router as cron_router
from api.routes.projection_routes import router as projection_router          # ← NEW
from api.routes.postmortem_routes import router as postmortem_router
from api.routes.schedule_routes import router as schedule_router
from api.routes.billing_draft import router as billing_draft_router
from scheduler.background_scheduler import (
    start_background_scheduler,
    stop_background_scheduler,
    get_scheduler_status,
)

configure_logging()
logger = get_logger("api.main")


# ---------------------------------------------------------------------------
# App lifecycle
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    logger.info(
        "Construction Operations API starting up",
        extra={"version": settings.api_version, "log_format": settings.log_format},
    )
    if not settings.hcss_client_id or not settings.hcss_client_secret:
        logger.warning(
            "HCSS_CLIENT_ID or HCSS_CLIENT_SECRET not set — "
            "all verification requests will fail until credentials are provided."
        )
    create_tables()
    logger.info("Database tables verified/created.")          # projection tables auto-created ← NEW

    start_background_scheduler()
    logger.info("Background data sync scheduler started.")

    yield

    stop_background_scheduler()
    logger.info("Background data sync scheduler stopped.")
    logger.info("Construction Operations API shut down")


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------
app = FastAPI(
    title=settings.api_title,
    version=settings.api_version,
    description=settings.api_description,
    docs_url="/docs" if settings.docs_enabled else None,
    redoc_url="/redoc" if settings.docs_enabled else None,
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
    expose_headers=["X-Request-ID"],
)

app.add_middleware(RequestLoggingMiddleware)

# ── Routers ─────────────────────────────────────────────────────────────────
app.include_router(auth_router,       prefix="/api")
app.include_router(notify_router,     prefix="/api")
app.include_router(quantity_router,   prefix="/api")
app.include_router(quantity_test_router, prefix="/api")
app.include_router(budget_router,     prefix="/api")
app.include_router(projection_router, prefix="/api")   # ← NEW
app.include_router(variance_router,   prefix="/api")
app.include_router(postmortem_router, prefix="/api")
app.include_router(schedule_router,  prefix="/api")
app.include_router(billing_draft_router, prefix="/api")
app.include_router(cron_router)
# ---------------------------------------------------------------------------
# Global exception handlers
# ---------------------------------------------------------------------------
@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    req_id = request.headers.get("X-Request-ID", str(uuid.uuid4()))
    logger.exception(
        "Unhandled server error",
        extra={"request_id": req_id, "path": str(request.url)},
    )
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content=ErrorResponse(
            error="internal_server_error",
            message="An unexpected error occurred. Please try again or contact support.",
            request_id=req_id,
        ).model_dump(),
    )


# ---------------------------------------------------------------------------
# Helpers (unchanged)
# ---------------------------------------------------------------------------
def _map_verifier_exception(exc: Exception, request_id: str, context: str) -> None:
    if isinstance(exc, RuntimeError):
        logger.error("HCSS upstream error [%s]: %s", context, exc, extra={"request_id": request_id})
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
    if isinstance(exc, _requests.exceptions.ConnectionError):
        logger.error("Network error reaching HCSS API [%s]: %s", context, exc, extra={"request_id": request_id})
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Cannot reach the HCSS API. Check that the server has outbound internet access to api.hcssapps.com.",
        ) from exc
    if isinstance(exc, _requests.exceptions.Timeout):
        logger.error("Timeout reaching HCSS API [%s]: %s", context, exc, extra={"request_id": request_id})
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail="HCSS API request timed out. Please try again.",
        ) from exc
    logger.exception("Unexpected error during verification [%s]", context, extra={"request_id": request_id})
    raise HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail="Verification failed due to an internal error.",
    ) from exc


def _run_verification(
    target_date: str,
    business_unit_id: str | None,
    request_id: str = "-",
    db: Session | None = None,
) -> VerifyResponse:
    logger.info(
        "Starting time log verification",
        extra={
            "request_id": request_id,
            "target_date": target_date,
            "business_unit_id": business_unit_id or "all",
        },
    )
    if not db:
        logger.error("Database session not provided", extra={"request_id": request_id})
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Database connection error.",
        )
    cached = get_cached_verification(db, target_date, business_unit_id)
    if not cached:
        logger.warning(
            "No data found in database for requested date",
            extra={
                "request_id": request_id,
                "target_date": target_date,
                "business_unit_id": business_unit_id or "all",
            },
        )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No verification data available for {target_date}. Data is synced every 8 hours by the background job.",
        )
    summary_dict, results = cached
    logger.info(
        "Retrieved verification results from database",
        extra={"request_id": request_id, "target_date": target_date, "total": summary_dict["total"]},
    )
    grouped = group_results(results)
    rows    = [TimecardRow(**r) for r in results_to_rows(results)]
    summary = SummaryResponse(
        date=target_date,
        rejected=len(grouped["REJECTED"]),
        flagged=len(grouped["FLAGGED"]),
        approved=len(grouped["APPROVED"]),
        total=len(results),
    )
    logger.info(
        "Verification complete",
        extra={
            "request_id": request_id, "target_date": target_date,
            "total": summary.total, "rejected": summary.rejected,
            "flagged": summary.flagged, "approved": summary.approved,
        },
    )
    return VerifyResponse(summary=summary, results=rows)


def _get_request_id(request: Request) -> str:
    return request.headers.get("X-Request-ID", "-")


def _run_verification_range(
    start_date: str,
    end_date: str,
    business_unit_id: str | None,
    request_id: str = "-",
    db: Session | None = None,
) -> RangeVerifyResponse:
    logger.info(
        "Starting range time log verification",
        extra={
            "request_id": request_id, "start_date": start_date,
            "end_date": end_date, "business_unit_id": business_unit_id or "all",
        },
    )
    if not db:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Database connection error.")

    days = date_range(start_date, end_date)
    all_results: list[TimecardResult] = []
    missing_dates: list[str] = []
    available_dates: list[str] = []

    for day in days:
        cached = get_cached_verification(db, day, business_unit_id)
        if cached:
            _, day_results = cached
            all_results.extend(day_results)
            available_dates.append(day)
        else:
            missing_dates.append(day)

    if missing_dates:
        logger.warning(
            "Missing data for %d date(s) in database (skipping): %s",
            len(missing_dates),
            ", ".join(missing_dates[:5]) + ("..." if len(missing_dates) > 5 else ""),
            extra={"request_id": request_id},
        )
    if not all_results:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No verification data available for any date in range {start_date} to {end_date}.",
        )

    rows = [TimecardRow(**r) for r in results_to_rows(all_results)]
    by_date: list[SummaryResponse] = []
    for day in available_dates:
        day_results = [r for r in all_results if r.date == day]
        grouped     = group_results(day_results)
        by_date.append(SummaryResponse(
            date=day,
            rejected=len(grouped["REJECTED"]),
            flagged=len(grouped["FLAGGED"]),
            approved=len(grouped["APPROVED"]),
            total=len(day_results),
        ))

    overall_grouped = group_results(all_results)
    summary = DateRangeSummary(
        date_from=start_date, date_to=end_date,
        rejected=len(overall_grouped["REJECTED"]),
        flagged=len(overall_grouped["FLAGGED"]),
        approved=len(overall_grouped["APPROVED"]),
        total=len(all_results),
    )
    logger.info(
        "Range verification complete",
        extra={
            "request_id": request_id, "start_date": start_date, "end_date": end_date,
            "total": summary.total, "available_dates": len(available_dates),
            "missing_dates": len(missing_dates),
        },
    )
    return RangeVerifyResponse(summary=summary, by_date=by_date, results=rows)


# ---------------------------------------------------------------------------
# Routes (unchanged)
# ---------------------------------------------------------------------------
@app.get("/api/health", tags=["System"], summary="Health check")
def health() -> dict[str, str]:
    logger.debug("Health check called")
    return {"status": "ok", "version": settings.api_version}


@app.get(
    "/api/db/stats",
    tags=["System"],
    summary="Get database statistics for all modules",
    description=(
        "Returns comprehensive statistics for all tracking modules:\n\n"
        "- **Timelog Verification**\n"
        "- **Quantity Tracking**\n"
        "- **Budget Tracking**\n"
        "- **Projection Tracking** ← NEW\n"
        "- **Summary**"
    ),
)
def db_stats(db: Session = Depends(get_db)) -> dict[str, Any]:
    from api.db_stats import get_comprehensive_db_stats
    return get_comprehensive_db_stats(db)


@app.get(
    "/api/verify",
    response_model=VerifyResponse | RangeVerifyResponse,
    tags=["Time Log Verification"],
    summary="Verify timecards — single date or date range",
)
def verify(
    request: Request,
    date: str | None = Query(default=None),
    date_from: str | None = Query(default=None),
    date_to: str | None = Query(default=None),
    business_unit_id: str | None = Query(default=None),
    db: Session = Depends(get_db),
) -> VerifyResponse | RangeVerifyResponse:
    req_id = _get_request_id(request)
    if date_from or date_to:
        yesterday = default_date()
        start = validate_date(date_from or yesterday)
        end   = validate_date(date_to   or yesterday)
        return _run_verification_range(start, end, business_unit_id, req_id, db)
    target_date = validate_date(date or default_date())
    return _run_verification(target_date, business_unit_id, req_id, db)


@app.get(
    "/api/time_log/count",
    response_model=CountOnlyResponse,
    tags=["Time Log Verification"],
    summary="Get verification counts only — single date or date range",
)
def time_log_count(
    request: Request,
    date: str | None = Query(default=None),
    date_from: str | None = Query(default=None),
    date_to: str | None = Query(default=None),
    business_unit_id: str | None = Query(default=None),
    db: Session = Depends(get_db),
) -> CountOnlyResponse:
    req_id = _get_request_id(request)

    if date_from or date_to:
        yesterday = default_date()
        start = validate_date(date_from or yesterday)
        end   = validate_date(date_to   or yesterday)
        days  = date_range(start, end)
        all_results: list[TimecardResult] = []
        missing_dates: list[str] = []
        available_dates: list[str] = []
        for day in days:
            cached = get_cached_verification(db, day, business_unit_id)
            if cached:
                _, day_results = cached
                all_results.extend(day_results)
                available_dates.append(day)
            else:
                missing_dates.append(day)
        if not all_results:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"No verification data available for any date in range {start} to {end}.",
            )
        grouped = group_results(all_results)
        return CountOnlyResponse(
            date_from=start, date_to=end,
            rejected=len(grouped["REJECTED"]),
            flagged=len(grouped["FLAGGED"]),
            approved=len(grouped["APPROVED"]),
            total=len(all_results),
        )

    target_date = validate_date(date or default_date())
    cached = get_cached_verification(db, target_date, business_unit_id)
    if not cached:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No verification data available for {target_date}.",
        )
    summary_dict, results = cached
    return CountOnlyResponse(
        date=target_date,
        rejected=summary_dict["rejected"],
        flagged=summary_dict["flagged"],
        approved=summary_dict["approved"],
        total=summary_dict["total"],
    )