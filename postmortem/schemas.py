"""
postmortem/schemas.py
──────────────────────
Pydantic request/response models for the Postmortem Analysis API.

Kept separate from the analyzer so routes and tests can import them
without pulling in the full aggregation engine.

Follows the same conventions as variance_detection/schemas.py and
api/routes/budget.py.
"""
from __future__ import annotations

from pydantic import BaseModel, Field


# ── Cost code sub-schemas ─────────────────────────────────────────────────────

class CostCodeSummarySchema(BaseModel):
    """Aggregated cost vs budget for one cost code within a project."""
    cost_code_id:          str         = Field(..., description="HCSS cost code UUID")
    cost_code:             str         = Field(..., description="Human-readable code, e.g. '01-100'")
    cost_code_description: str         = Field(..., description="Description, e.g. 'Site Preparation'")
    expected_budget:       float       = Field(..., description="Planned budget ($)")
    actual_cost:           float       = Field(..., description="Actual cost to date ($)")
    variance:              float       = Field(..., description="expected − actual ($). Positive = under budget.")
    utilization_pct:       float | None = Field(None, description="(actual / expected) × 100. null when budget is 0.")
    loss_amount:           float       = Field(default=0.0, description="max(0, actual − expected) ($)")
    loss_pct:              float | None = Field(None, description="(loss / expected) × 100. null when budget is 0.")
    status:                str         = Field(..., description="ON_TRACK | OVER_RISK")


# ── Quantity sub-schemas ──────────────────────────────────────────────────────

class QuantityCodeSummarySchema(BaseModel):
    """Aggregated quantity progress for one cost code within a project."""
    cost_code_id:       str   = Field(..., description="HCSS cost code UUID")
    cost_code:          str   = Field(..., description="Human-readable code")
    description:        str   = Field(..., description="Cost code description")
    unit:               str   = Field(..., description="Unit of measure, e.g. 'CY', 'LF'")
    cost_type:          str   = Field(..., description="self_perform | subcontractor")
    planned_quantity:   float = Field(..., description="Planned / budgeted quantity")
    installed_quantity: float = Field(..., description="Quantity installed to date")
    remaining_quantity: float = Field(..., description="planned − installed (can be negative)")
    percent_complete:   float = Field(..., description="(installed / planned) × 100")
    status:             str   = Field(..., description="ON_TRACK | NEAR_COMPLETION | OVER_RISK")
    alert:              bool  = Field(..., description="True when percent_complete >= 75%")


# ── Missed cost sub-schema ────────────────────────────────────────────────────

class MissedCostItemSchema(BaseModel):
    """A cost code flagged as a potential missed or unrecorded cost."""
    cost_code_id:           str         = Field(..., description="HCSS cost code UUID, or 'TIMELOG' for timelog-sourced items")
    cost_code:              str         = Field(..., description="Human-readable code")
    description:            str         = Field(..., description="Cost code description")
    qty_percent_complete:   float       = Field(..., description="Quantity percent complete at time of detection")
    cost_utilization_pct:   float | None = Field(None, description="Cost utilisation % at time of detection")
    expected_budget:        float       = Field(..., description="Planned budget ($)")
    actual_cost:            float       = Field(..., description="Actual cost recorded ($)")
    estimated_missed_value: float       = Field(..., description="Estimated unrecorded cost = expected − actual ($)")
    source:                 str         = Field(..., description="qty_vs_cost | rejected_timecards")
    detail:                 str         = Field(..., description="Human-readable explanation of the flag")


# ── Timelog sub-schema ────────────────────────────────────────────────────────

class TimelogSummarySchema(BaseModel):
    """Time log quality summary for a project."""
    total_timecards:   int        = Field(..., description="Total timecards processed for this project")
    approved:          int        = Field(..., description="Approved timecards")
    flagged:           int        = Field(..., description="Flagged timecards (soft warnings)")
    rejected:          int        = Field(..., description="Rejected timecards (hard failures)")
    rejection_reasons: list[str]  = Field(default_factory=list, description="Deduplicated rejection reasons, most common first")
    flag_reasons:      list[str]  = Field(default_factory=list, description="Deduplicated flag reasons, most common first")
    quality_score_pct: float      = Field(..., description="(approved / total) × 100")


# ── Projection sub-schema ─────────────────────────────────────────────────────

class ProjectionSummarySchema(BaseModel):
    """Latest monthly projection snapshot for a project."""
    tracking_month:             str         = Field(..., description="Month this projection was calculated (YYYY-MM)")
    original_budget:            float       = Field(..., description="Original bid / estimate ($)")
    actual_cost_to_date:        float       = Field(..., description="Costs incurred to date ($)")
    projected_final_cost:       float       = Field(..., description="Estimate at Completion — EAC ($)")
    cost_variance:              float       = Field(..., description="original_budget − projected_final_cost ($). Negative = over budget.")
    percent_complete:           float       = Field(..., description="Estimated % complete based on cost burn rate")
    original_contract_value:    float       = Field(..., description="Original contract / SOV amount ($)")
    approved_change_orders:     float       = Field(..., description="Sum of approved change orders ($)")
    revised_contract_value:     float       = Field(..., description="original_contract + approved_COs ($)")
    billed_to_date:             float       = Field(..., description="Amount invoiced to owner to date ($)")
    projected_final_billing:    float       = Field(..., description="Expected total billing at completion ($)")
    billing_variance:           float       = Field(..., description="billed_to_date − actual_cost_to_date ($). Negative = cash risk.")
    estimated_completion_month: str         = Field(..., description="Estimated project completion month (YYYY-MM)")
    status:                     str         = Field(..., description="ON_TRACK | AT_RISK | OVER_BUDGET")
    alert:                      bool        = Field(..., description="True when status is not ON_TRACK or discrepancy detected")
    discrepancy_flag:           bool        = Field(..., description="True when projected final cost diverges > 5% from original budget")


# ── Deferred section stub ─────────────────────────────────────────────────────

class DeferredSectionSchema(BaseModel):
    """
    Placeholder for sections that depend on modules not yet complete.
    Returned in the API response so the frontend can render a stub tab.
    """
    available:  bool = Field(default=False, description="False until the source module is complete")
    depends_on: str  = Field(..., description="Name of the module this section depends on")
    message:    str  = Field(..., description="Human-readable explanation for the frontend")


# ── Main response schemas ─────────────────────────────────────────────────────

class PostmortemProjectRow(BaseModel):
    """
    One row in the postmortem project list table.
    Contains only the KPI fields needed for the summary view.
    """
    job_id:                      str         = Field(..., description="HCSS job UUID")
    job_code:                    str         = Field(..., description="Human-readable job code")
    job_name:                    str         = Field(..., description="Full job name")
    business_unit:               str         = Field(..., description="Business unit")
    overall_risk:                str         = Field(..., description="ON_TRACK | AT_RISK | OVER_BUDGET")
    total_expected_budget:       float       = Field(..., description="Total planned budget ($)")
    total_actual_cost:           float       = Field(..., description="Total actual cost ($)")
    total_variance:              float       = Field(..., description="expected − actual ($)")
    total_variance_pct:          float | None = Field(None, description="Cost utilisation %. null when budget is 0.")
    total_loss_amount:           float       = Field(..., description="Total loss amount ($)")
    total_loss_pct:              float | None = Field(None, description="Loss as % of budget. null when budget is 0.")
    over_risk_cost_codes:        int         = Field(..., description="Number of cost codes at OVER_RISK")
    near_completion_qty_codes:   int         = Field(..., description="Number of quantity codes at NEAR_COMPLETION")
    over_risk_qty_codes:         int         = Field(..., description="Number of quantity codes at OVER_RISK")
    missed_cost_count:           int         = Field(..., description="Number of missed cost items detected")
    estimated_missed_cost_total: float       = Field(..., description="Total estimated missed cost ($)")
    timelog_quality_score_pct:   float | None = Field(None, description="Timelog quality score %. null when no timelog data.")
    projection_status:           str | None  = Field(None, description="Latest projection status. null when no projection data.")
    projection_percent_complete: float | None = Field(None, description="Latest projection % complete. null when no projection data.")


class PostmortemListResponse(BaseModel):
    """Response for GET /api/postmortem/list — all projects summary table."""
    total_projects:    int                      = Field(..., description="Total number of projects with data")
    on_track:          int                      = Field(..., description="Projects with ON_TRACK overall risk")
    at_risk:           int                      = Field(..., description="Projects with AT_RISK overall risk")
    over_budget:       int                      = Field(..., description="Projects with OVER_BUDGET overall risk")
    total_missed_cost: float                    = Field(..., description="Sum of estimated missed costs across all projects ($)")
    projects:          list[PostmortemProjectRow] = Field(..., description="Per-project summary rows")


class PostmortemDetailResponse(BaseModel):
    """
    Response for GET /api/postmortem/detail?job_id=xxx
    Full postmortem for a single project with all sections.
    """
    # ── Identity ──────────────────────────────────────────────────────
    job_id:        str = Field(..., description="HCSS job UUID")
    job_code:      str = Field(..., description="Human-readable job code")
    job_name:      str = Field(..., description="Full job name")
    business_unit: str = Field(..., description="Business unit")

    # ── Overall KPIs ──────────────────────────────────────────────────
    overall_risk:                str         = Field(..., description="ON_TRACK | AT_RISK | OVER_BUDGET")
    total_expected_budget:       float       = Field(..., description="Total planned budget ($)")
    total_actual_cost:           float       = Field(..., description="Total actual cost ($)")
    total_variance:              float       = Field(..., description="expected − actual ($)")
    total_variance_pct:          float | None = Field(None, description="Cost utilisation %")
    total_loss_amount:           float       = Field(..., description="Total loss amount ($)")
    total_loss_pct:              float | None = Field(None, description="Loss as % of budget")
    over_risk_cost_codes:        int         = Field(..., description="Cost codes at OVER_RISK")
    near_completion_qty_codes:   int         = Field(..., description="Quantity codes at NEAR_COMPLETION")
    over_risk_qty_codes:         int         = Field(..., description="Quantity codes at OVER_RISK")
    missed_cost_count:           int         = Field(..., description="Missed cost items detected")
    estimated_missed_cost_total: float       = Field(..., description="Total estimated missed cost ($)")

    # ── Section data ──────────────────────────────────────────────────
    cost_codes:   list[CostCodeSummarySchema]    = Field(default_factory=list, description="Cost vs budget per cost code")
    quantities:   list[QuantityCodeSummarySchema] = Field(default_factory=list, description="Quantity progress per cost code")
    missed_costs: list[MissedCostItemSchema]      = Field(default_factory=list, description="Detected missed / unrecorded costs")
    timelog:      TimelogSummarySchema | None     = Field(None, description="Time log quality summary. null when no data.")
    projection:   ProjectionSummarySchema | None  = Field(None, description="Latest monthly projection. null when no data.")

    # ── Deferred sections ─────────────────────────────────────────────
    change_orders:        DeferredSectionSchema = Field(
        default_factory=lambda: DeferredSectionSchema(
            available=False,
            depends_on="Billing Draft",
            message="Change order data will be available once the Billing Draft module is complete.",
        ),
        description="Change order analysis — deferred pending Billing Draft module.",
    )
    schedule_performance: DeferredSectionSchema = Field(
        default_factory=lambda: DeferredSectionSchema(
            available=False,
            depends_on="Schedule Monitoring",
            message="Schedule performance data will be available once the Schedule Monitoring module is complete.",
        ),
        description="Schedule performance — deferred pending Schedule Monitoring module.",
    )


class PostmortemNotifyRequest(BaseModel):
    """Request body for POST /api/postmortem/notify."""
    job_id:     str         = Field(..., description="HCSS job UUID to include in the report")
    recipients: list[str]   = Field(..., min_length=1, description="Email recipient list")
    comments:   str         = Field(default="", description="Optional comments to include in the email")


class PostmortemNotifyResponse(BaseModel):
    """Response for POST /api/postmortem/notify."""
    job_id:            str = Field(..., description="HCSS job UUID")
    job_code:          str = Field(..., description="Human-readable job code")
    recipients_count:  int = Field(..., description="Number of recipients notified")
    message:           str = Field(..., description="Confirmation message")
