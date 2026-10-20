"""
Variance Detection — Pydantic request/response schemas.

Kept in a dedicated module so they can be imported by routes and tests
without pulling in the full detector engine.
"""
from __future__ import annotations

from pydantic import BaseModel, Field


# ── Shared ────────────────────────────────────────────────────────────────────

class VarianceSummarySchema(BaseModel):
    period_start: str = Field(..., description="Start of the queried period (YYYY-MM-DD)")
    period_end:   str = Field(..., description="End of the queried period (YYYY-MM-DD)")
    total_items:  int = Field(..., description="Total cost codes / items returned")
    at_risk:      int = Field(..., description="Items flagged OVER_RISK (or NEAR_COMPLETION for quantity)")


# ── Cost Variance ─────────────────────────────────────────────────────────────

class CostVarianceSummary(VarianceSummarySchema):
    """Summary block for the cost variance response."""
    total_expected_budget: float = Field(
        ..., description="Sum of expected budgets across all returned items ($)"
    )
    total_actual_cost: float = Field(
        ..., description="Sum of actual costs across all returned items ($)"
    )
    total_cost_variance: float = Field(
        ...,
        description=(
            "total_actual_cost − total_expected_budget. "
            "Positive = over budget overall, negative = under budget."
        ),
    )

    model_config = {"json_schema_extra": {
        "example": {
            "period_start": "2026-05-01",
            "period_end":   "2026-05-12",
            "total_items":  42,
            "at_risk":      7,
            "total_expected_budget": 1_250_000.00,
            "total_actual_cost":     1_312_500.00,
            "total_cost_variance":      62_500.00,
        }
    }}


class CostVarianceItemSchema(BaseModel):
    """Single cost-code row in the cost variance response."""
    cost_code_id:          str   = Field(..., description="HCSS cost code UUID")
    cost_code:             str   = Field(..., description="Human-readable code, e.g. '01-100'")
    cost_code_description: str   = Field(..., description="Description, e.g. 'Site Preparation'")
    job_id:                str   = Field(..., description="HCSS job UUID")
    job_name:              str   = Field(..., description="Human-readable job name")
    business_unit:         str   = Field(..., description="Business unit name")
    budgeted_all_cost:     float = Field(
        ...,
        description="Total HCSS planned budget (all cost-type dollars). Raw stored value.",
    )
    expected_budget:       float = Field(
        ...,
        description=(
            "(installed_quantity / planned_quantity) × budgeted_all_cost. "
            "Zero when no quantity data is available."
        ),
    )
    actual_cost:           float = Field(..., description="Current actual cost ($)")
    cost_variance_amount:  float = Field(
        ...,
        description=(
            "expected_budget − actual_cost ($). "
            "Positive = under budget, negative = over budget."
        ),
    )
    cost_variance_pct: float | None = Field(
        default=None,
        description="(actual_cost / expected_budget) × 100. null when expected_budget is 0.",
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

    model_config = {"json_schema_extra": {
        "example": {
            "cost_code_id":          "cc-uuid-001",
            "cost_code":             "01-100",
            "cost_code_description": "Site Preparation",
            "job_id":                "job-uuid-001",
            "job_name":              "Highway 89 Widening",
            "business_unit":         "Heavy Civil",
            "expected_budget":       50_000.00,
            "actual_cost":           47_500.00,
            "cost_variance_amount":  -2_500.00,
            "cost_variance_pct":     95.0,
            "status":                "OVER_RISK",
        }
    }}


class CostVarianceResponse(BaseModel):
    """Full response for GET /variance/cost."""
    summary: CostVarianceSummary
    items:   list[CostVarianceItemSchema]


# ── Quantity Variance ─────────────────────────────────────────────────────────

class QuantityVarianceSummary(VarianceSummarySchema):
    """Summary block for the quantity variance response."""
    near_completion: int = Field(
        ..., description="Items at NEAR_COMPLETION (>= 75 % complete)"
    )
    over_risk: int = Field(
        ..., description="Items at OVER_RISK (>= 100 % complete / over-run)"
    )

    model_config = {"json_schema_extra": {
        "example": {
            "period_start":    "2026-05-01",
            "period_end":      "2026-05-12",
            "total_items":     38,
            "at_risk":         9,
            "near_completion": 6,
            "over_risk":       3,
        }
    }}


class QuantityVarianceItemSchema(BaseModel):
    """Single cost-code row in the quantity variance response."""
    cost_code_id:        str   = Field(..., description="HCSS cost code UUID")
    cost_code:           str   = Field(..., description="Human-readable code")
    description:         str   = Field(..., description="Cost code description")
    job_id:              str   = Field(..., description="HCSS job UUID")
    job_code:            str   = Field(..., description="Human-readable job code")
    unit:                str   = Field(..., description="Unit of measure, e.g. 'CY', 'LF'")
    cost_type:           str   = Field(..., description="self_perform | subcontractor")
    planned_quantity:    float = Field(..., description="Planned / budgeted quantity")
    installed_quantity:  float = Field(..., description="Quantity installed to date")
    remaining_quantity:  float = Field(..., description="planned − installed (can be negative)")
    qty_variance_amount: float = Field(
        ...,
        description=(
            "installed_quantity − planned_quantity. "
            "Positive = over-run, negative = work remaining."
        ),
    )
    qty_variance_pct: float = Field(
        ...,
        description="(installed_quantity / planned_quantity) × 100. Percent complete.",
    )
    status: str  = Field(..., description="ON_TRACK | NEAR_COMPLETION | OVER_RISK")
    alert:  bool = Field(..., description="True when qty_variance_pct >= 75 %")

    model_config = {"json_schema_extra": {
        "example": {
            "cost_code_id":        "cc-uuid-002",
            "cost_code":           "02-200",
            "description":         "Earthwork Excavation",
            "job_id":              "job-uuid-001",
            "job_code":            "HWY-89",
            "unit":                "CY",
            "cost_type":           "self_perform",
            "planned_quantity":    10_000.0,
            "installed_quantity":  8_200.0,
            "remaining_quantity":  1_800.0,
            "qty_variance_amount": -1_800.0,
            "qty_variance_pct":    82.0,
            "status":              "NEAR_COMPLETION",
            "alert":               True,
        }
    }}


class QuantityVarianceResponse(BaseModel):
    """Full response for GET /variance/quantity."""
    summary: QuantityVarianceSummary
    items:   list[QuantityVarianceItemSchema]


# ── Billing Variance (stub) ───────────────────────────────────────────────────

class BillingVarianceResponse(BaseModel):
    """
    Stub response for GET /variance/billing.
    Returns an empty items list until the billing module is implemented.
    """
    not_implemented: bool = Field(
        default=True,
        description="Billing variance is not yet implemented.",
    )
    message: str = Field(
        default=(
            "Billing variance detection is planned for a future phase. "
            "No data is available yet."
        ),
    )
    period_start: str = Field(..., description="Requested period start (YYYY-MM-DD)")
    period_end:   str = Field(..., description="Requested period end (YYYY-MM-DD)")
    items:        list = Field(default_factory=list, description="Always empty for now.")

    model_config = {"json_schema_extra": {
        "example": {
            "not_implemented": True,
            "message":         "Billing variance detection is planned for a future phase.",
            "period_start":    "2026-05-01",
            "period_end":      "2026-05-12",
            "items":           [],
        }
    }}
