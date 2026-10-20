"""
Notification routes:
  POST /notify/send             — bulk daily report email (all timecards for a date)
  POST /notify/send-individual  — single timecard email (per-row "Send Email" button)
  GET  /notify/recipients       — list of hardcoded recipient options for the modal
"""
from __future__ import annotations

import logging
from datetime import date as date_type, timedelta

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, EmailStr, Field

from api.schemas import ErrorResponse
from notifications.email_sender import send_time_log_report_email
from notifications.email_template import build_email_body
from notifications.individual_email_sender import get_recipient_directory, send_individual_email
from notifications.individual_email_template import build_individual_email_body
from payroll_verification.verifier import verify_payroll_date

router = APIRouter(prefix="/notify", tags=["Notifications"])
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Response / Request schemas
# ---------------------------------------------------------------------------

class SendEmailResponse(BaseModel):
    report_date: str
    recipients_notified: bool
    rejected: int
    flagged: int
    approved: int
    total: int
    message: str


class RecipientOption(BaseModel):
    role: str = Field(..., description="Role key (supervisor, project_manager, admin)")
    email: str = Field(..., description="Email address for this role")


class RecipientsResponse(BaseModel):
    recipients: list[RecipientOption]


class SendIndividualRequest(BaseModel):
    timecard_id: str = Field(..., description="HCSS timecard UUID")
    date: str = Field(..., description="Timecard date YYYY-MM-DD")
    job_code: str = Field(..., description="Job code")
    foreman: str = Field(..., description="Foreman name")
    status: str = Field(..., description="APPROVED | FLAGGED | REJECTED")
    reasons: list[str] = Field(default_factory=list, description="Rejection reasons")
    flags: list[str] = Field(default_factory=list, description="Warning flags")
    recipients: list[EmailStr] = Field(
        ...,
        min_length=1,
        description="Email addresses to send to (selected from modal + any custom address)",
    )
    comments: str = Field(default="", description="Optional user comments to include in email")


class SendIndividualResponse(BaseModel):
    timecard_id: str
    recipients_count: int
    message: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _default_date() -> str:
    return (date_type.today() - timedelta(days=1)).strftime("%Y-%m-%d")


def _validate_date(date_str: str) -> str:
    try:
        date_type.fromisoformat(date_str)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid date '{date_str}'. Expected format: YYYY-MM-DD.",
        )
    return date_str


# ---------------------------------------------------------------------------
# Route 1 — Bulk daily report (existing, unchanged)
# ---------------------------------------------------------------------------

@router.post(
    "/send",
    response_model=SendEmailResponse,
    summary="Trigger a time log verification email immediately",
    description=(
        "Runs time log verification for the given date and sends the HTML report "
        "email to all configured EMAIL_RECIPIENTS. "
        "Used by the frontend 'Send Email Now' button."
    ),
    responses={
        422: {"model": ErrorResponse, "description": "Invalid date format"},
        502: {"model": ErrorResponse, "description": "HCSS upstream error or SMTP failure"},
    },
)
def send_email_now(
    date: str | None = Query(
        default=None,
        description="Report date YYYY-MM-DD (default: yesterday)",
    ),
    business_unit_id: str | None = Query(
        default=None,
        description="Filter by business unit ID (optional)",
    ),
) -> SendEmailResponse:
    target_date = _validate_date(date or _default_date())

    logger.info("Manual email send triggered for date=%s", target_date)

    try:
        results = verify_payroll_date(
            target_date=target_date,
            business_unit_id=business_unit_id,
        )
    except RuntimeError as exc:
        logger.error("HCSS error during manual email send: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"HCSS upstream error: {exc}",
        ) from exc
    except Exception as exc:
        logger.exception("Unexpected error during verification for email send")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Verification failed due to an internal error.",
        ) from exc

    subject, html_body = build_email_body(results, target_date)

    try:
        send_time_log_report_email(subject, html_body)
        recipients_notified = True
        logger.info("Manual email sent successfully for date=%s", target_date)
    except Exception as exc:
        logger.error("SMTP failure during manual email send: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Email send failed: {exc}",
        ) from exc

    rejected = sum(1 for r in results if r.status == "REJECTED")
    flagged = sum(1 for r in results if r.status == "FLAGGED")
    approved = sum(1 for r in results if r.status == "APPROVED")

    return SendEmailResponse(
        report_date=target_date,
        recipients_notified=recipients_notified,
        rejected=rejected,
        flagged=flagged,
        approved=approved,
        total=len(results),
        message=f"Email report for {target_date} sent successfully.",
    )


# ---------------------------------------------------------------------------
# Route 2 — Recipient directory for frontend modal
# ---------------------------------------------------------------------------

@router.get(
    "/recipients",
    response_model=RecipientsResponse,
    summary="List hardcoded recipient options for the Send Email modal",
    description=(
        "Returns the list of pre-configured email recipients "
        "(Direct Supervisor, Project Manager, Admin Team) loaded from .env. "
        "The frontend uses this to populate the modal checkboxes."
    ),
)
def list_recipients() -> RecipientsResponse:
    logger.debug("Fetching recipient directory from .env")
    directory = get_recipient_directory()

    options = [
        RecipientOption(role=role, email=email)
        for role, email in directory.items()
    ]

    logger.info("Returning %d recipient option(s)", len(options))
    return RecipientsResponse(recipients=options)


# ---------------------------------------------------------------------------
# Route 3 — Per-row individual timecard email
# ---------------------------------------------------------------------------

@router.post(
    "/send-individual",
    response_model=SendIndividualResponse,
    summary="Send email for a single timecard (per-row Send Email button)",
    description=(
        "Sends an HTML email for one specific timecard to the selected recipients. "
        "The request body contains the timecard details, selected recipients, "
        "and optional user comments. Used by the per-row Send Email modal."
    ),
    responses={
        422: {"model": ErrorResponse, "description": "Invalid request data"},
        502: {"model": ErrorResponse, "description": "SMTP failure"},
    },
)
def send_individual(payload: SendIndividualRequest) -> SendIndividualResponse:
    logger.info(
        "Individual email send triggered for timecard_id=%s job=%s foreman=%s status=%s recipients=%d",
        payload.timecard_id,
        payload.job_code,
        payload.foreman,
        payload.status,
        len(payload.recipients),
    )

    _validate_date(payload.date)

    valid_statuses = {"APPROVED", "FLAGGED", "REJECTED"}
    if payload.status.upper() not in valid_statuses:
        logger.warning(
            "Invalid status '%s' for timecard_id=%s",
            payload.status,
            payload.timecard_id,
        )
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid status '{payload.status}'. Must be one of: {', '.join(valid_statuses)}.",
        )

    subject, html_body = build_individual_email_body(
        timecard_id=payload.timecard_id,
        date=payload.date,
        job_code=payload.job_code,
        foreman=payload.foreman,
        status=payload.status,
        reasons=payload.reasons,
        flags=payload.flags,
        comments=payload.comments,
    )

    logger.debug(
        "Email built for timecard_id=%s subject=%s",
        payload.timecard_id,
        subject,
    )

    recipient_list = [str(email) for email in payload.recipients]
    try:
        send_individual_email(
            subject=subject,
            html_body=html_body,
            recipients=recipient_list,
        )
    except ValueError as exc:
        logger.error(
            "SMTP config error during individual send for timecard_id=%s: %s",
            payload.timecard_id,
            exc,
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Email configuration error: {exc}",
        ) from exc
    except Exception as exc:
        logger.error(
            "SMTP failure during individual send for timecard_id=%s: %s",
            payload.timecard_id,
            exc,
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Email send failed: {exc}",
        ) from exc

    logger.info(
        "Individual email sent successfully for timecard_id=%s to %d recipient(s)",
        payload.timecard_id,
        len(recipient_list),
    )

    return SendIndividualResponse(
        timecard_id=payload.timecard_id,
        recipients_count=len(recipient_list),
        message=f"Email for timecard {payload.timecard_id} sent to {len(recipient_list)} recipient(s).",
    )