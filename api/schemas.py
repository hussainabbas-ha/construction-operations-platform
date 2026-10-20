"""
Pydantic response/error models for Construction Operations API - Time Log Verification module.
Keeping schemas in a dedicated module makes them easy to share with tests.
"""
from __future__ import annotations

from pydantic import BaseModel, EmailStr, Field, field_validator


class TimecardRow(BaseModel):
    # extra fields produced upstream (e.g. business_unit_id, job_id) are ignored
    model_config = {"extra": "ignore"}

    id: str = Field(..., description="HCSS timecard UUID")
    date: str = Field(..., description="Timecard date (YYYY-MM-DD)")
    job_code: str = Field(..., description="Human-readable job code")
    job_description: str = Field("", description="Job description")
    foreman_id: str | None = Field(None, description="Foreman UUID")
    foreman: str = Field(..., description="Foreman display name")
    status: str = Field(..., description="APPROVED | FLAGGED | REJECTED")
    reasons: list[str] = Field(
        ...,
        description=(
            "Flat list of human-readable issue messages for this card's status: "
            "reject reasons when REJECTED, warnings when FLAGGED, [] when APPROVED. "
            "Cost-code names are embedded in the message text."
        ),
    )
    portal_sync_status: str = Field(
        "pending",
        description=(
            "Whether this card has been synced to HeavyJob by the automation: "
            "pending | approved | rejected | failed | not_found | skipped"
        ),
    )
    portal_synced_at: str | None = Field(
        None, description="ISO timestamp of the last portal sync attempt (null if never)"
    )


class SummaryResponse(BaseModel):
    date: str = Field(..., description="Verification date (YYYY-MM-DD)")
    rejected: int = Field(..., description="Number of rejected timecards")
    flagged: int = Field(..., description="Number of flagged timecards")
    approved: int = Field(..., description="Number of approved timecards")
    total: int = Field(..., description="Total timecards processed")

    model_config = {"json_schema_extra": {
        "example": {
            "date": "2026-04-14",
            "rejected": 3,
            "flagged": 7,
            "approved": 42,
            "total": 52,
        }
    }}


class VerifyResponse(BaseModel):
    summary: SummaryResponse
    results: list[TimecardRow]


class DateRangeSummary(BaseModel):
    date_from: str = Field(..., description="Start of the verification range (YYYY-MM-DD)")
    date_to: str = Field(..., description="End of the verification range (YYYY-MM-DD)")
    rejected: int = Field(..., description="Total rejected timecards across all dates")
    flagged: int = Field(..., description="Total flagged timecards across all dates")
    approved: int = Field(..., description="Total approved timecards across all dates")
    total: int = Field(..., description="Total timecards processed across all dates")


class RangeVerifyResponse(BaseModel):
    summary: DateRangeSummary
    by_date: list[SummaryResponse] = Field(..., description="Per-day breakdown")
    results: list[TimecardRow]


class ErrorResponse(BaseModel):
    error: str = Field(..., description="Machine-readable error code")
    message: str = Field(..., description="Human-readable error description")
    request_id: str = Field(..., description="Request trace ID for support")

    model_config = {"json_schema_extra": {
        "example": {
            "error": "internal_server_error",
            "message": "An unexpected error occurred.",
            "request_id": "a1b2c3d4-...",
        }
    }}


# ---------------------------------------------------------------------------
# Auth schemas
# ---------------------------------------------------------------------------

class SignupRequest(BaseModel):
    email: EmailStr = Field(..., description="User email address")
    full_name: str = Field(..., min_length=2, max_length=100, description="Full name")
    password: str = Field(..., min_length=8, description="Password (min 8 characters)")

    @field_validator("password")
    @classmethod
    def password_strength(cls, v: str) -> str:
        if not any(c.isdigit() for c in v):
            raise ValueError("Password must contain at least one number.")
        if not any(c.isalpha() for c in v):
            raise ValueError("Password must contain at least one letter.")
        return v


class LoginRequest(BaseModel):
    email: EmailStr = Field(..., description="User email address")
    password: str = Field(..., description="User password")


class UserResponse(BaseModel):
    id: str
    email: str
    full_name: str
    is_active: bool

    model_config = {"from_attributes": True}


class TokenResponse(BaseModel):
    access_token: str = Field(..., description="JWT bearer token")
    token_type: str = Field(default="bearer")
    user: UserResponse


# ---------------------------------------------------------------------------
# Notification schemas
# ---------------------------------------------------------------------------

class SendEmailResponse(BaseModel):
    report_date: str
    recipients_notified: bool
    rejected: int
    flagged: int
    approved: int
    total: int
    message: str


class CountOnlyResponse(BaseModel):
    """Response model for count-only verification endpoint."""
    date: str | None = Field(None, description="Single verification date (YYYY-MM-DD)")
    date_from: str | None = Field(None, description="Range start date (YYYY-MM-DD)")
    date_to: str | None = Field(None, description="Range end date (YYYY-MM-DD)")
    rejected: int = Field(..., description="Number of rejected timecards")
    flagged: int = Field(..., description="Number of flagged timecards")
    approved: int = Field(..., description="Number of approved timecards")
    total: int = Field(..., description="Total timecards processed")

    model_config = {"json_schema_extra": {
        "examples": [
            {
                "date": "2026-04-27",
                "date_from": None,
                "date_to": None,
                "rejected": 3,
                "flagged": 7,
                "approved": 42,
                "total": 52,
            },
            {
                "date": None,
                "date_from": "2026-04-20",
                "date_to": "2026-04-27",
                "rejected": 15,
                "flagged": 28,
                "approved": 210,
                "total": 253,
            }
        ]
    }}

