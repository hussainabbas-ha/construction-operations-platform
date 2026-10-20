"""
api/database.py
────────────────
CHANGES FROM ORIGINAL:
  + Added ProjectionTrackingCache model   (new — projection tracking module)
  + Added ProjectionTrackingResult model  (new — projection tracking module)

Search for "← NEW" comments to find every change in this file.
All existing code is unchanged.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

from dotenv import load_dotenv
from pathlib import Path

from sqlalchemy import (
    Boolean, Column, DateTime, Float, Integer,
    String, Text, create_engine, Index, ForeignKey,
)
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker, relationship

_REPO_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(_REPO_ROOT / ".env")

DATABASE_URL = os.getenv("DATABASE_URL", "")

if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is not set in .env")

if DATABASE_URL.startswith("postgresql://") or DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+psycopg://", 1)
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+psycopg://", 1)

engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
    pool_size=5,
    max_overflow=10,
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


# ===========================================================================
# USER MODEL
# ===========================================================================

class User(Base):
    __tablename__ = "users"

    id             = Column(String, primary_key=True, index=True)
    email          = Column(String, unique=True, index=True, nullable=False)
    full_name      = Column(String, nullable=False)
    hashed_password = Column(String, nullable=False)
    is_active      = Column(Boolean, default=True, nullable=False)
    created_at     = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)


# ===========================================================================
# TIME LOG VERIFICATION MODELS
# ===========================================================================

class TimelogVerificationCache(Base):
    __tablename__ = "timelog_verification_cache"

    id                = Column(String, primary_key=True, index=True)
    verification_date = Column(String, nullable=False, index=True)
    business_unit_id  = Column(String, nullable=True, index=True)

    total_timecards = Column(Integer, nullable=False, default=0)
    approved_count  = Column(Integer, nullable=False, default=0)
    flagged_count   = Column(Integer, nullable=False, default=0)
    rejected_count  = Column(Integer, nullable=False, default=0)

    fetched_at  = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
    created_at  = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
    updated_at  = Column(DateTime, default=lambda: datetime.now(timezone.utc),
                         onupdate=lambda: datetime.now(timezone.utc), nullable=False)

    __table_args__ = (
        Index('idx_timelog_date_bu', 'verification_date', 'business_unit_id', unique=True),
    )


class TimelogTimecardResult(Base):
    __tablename__ = "timelog_timecard_results"

    id                = Column(String, primary_key=True, index=True)
    verification_date = Column(String, nullable=False, index=True)
    business_unit_id  = Column(String, nullable=True, index=True)

    timecard_id = Column(String, nullable=False, index=True)
    job_id          = Column(String, nullable=True)
    job_code        = Column(String, nullable=False)
    job_description = Column(String, nullable=True, default="")
    foreman_id  = Column(String, nullable=True)
    foreman     = Column(String, nullable=False)

    status  = Column(String, nullable=False, index=True)
    reasons = Column(Text, nullable=False, default="")
    flags   = Column(Text, nullable=False, default="")
    why     = Column(Text, nullable=False, default="")
    # structured per-issue detail (JSON list); see verifier.TimecardResult.issues
    issues  = Column(Text, nullable=True, default="[]")
    # HeavyJob portal sync status set by the browser automation (12:30 job):
    #   pending | approved | rejected | failed | not_found | skipped
    portal_sync_status = Column(String, nullable=False, default="pending", index=True)
    portal_synced_at   = Column(DateTime, nullable=True)

    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc),
                        onupdate=lambda: datetime.now(timezone.utc), nullable=False)

    __table_args__ = (
        Index('idx_timelog_timecard_date_bu', 'verification_date', 'business_unit_id'),
        Index('idx_timelog_timecard_status',  'status'),
        Index('idx_timelog_timecard_id',      'timecard_id'),
    )


# ===========================================================================
# QUANTITY TRACKING MODELS
# ===========================================================================

class QuantityTrackingCache(Base):
    __tablename__ = "quantity_tracking_cache"

    id            = Column(String, primary_key=True, index=True)
    tracking_date = Column(String, nullable=False, index=True)

    total_cost_codes      = Column(Integer, nullable=False, default=0)
    on_track_count        = Column(Integer, nullable=False, default=0)
    near_completion_count = Column(Integer, nullable=False, default=0)
    over_risk_count       = Column(Integer, nullable=False, default=0)

    fetched_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc),
                        onupdate=lambda: datetime.now(timezone.utc), nullable=False)

    __table_args__ = (
        Index('idx_quantity_date', 'tracking_date', unique=True),
    )


class QuantityTrackingResult(Base):
    __tablename__ = "quantity_tracking_results"

    id            = Column(String, primary_key=True, index=True)
    tracking_date = Column(String, nullable=False, index=True)

    cost_code_id = Column(String, nullable=False, index=True)
    cost_code    = Column(String, nullable=False)
    description  = Column(String, nullable=False)
    job_id       = Column(String, nullable=False, index=True)
    job_code     = Column(String, nullable=False)
    unit         = Column(String, nullable=False)
    cost_type    = Column(String, nullable=False, index=True)

    planned_quantity   = Column(Float, nullable=False, default=0.0)
    installed_quantity = Column(Float, nullable=False, default=0.0)
    remaining_quantity = Column(Float, nullable=False, default=0.0)
    percent_complete   = Column(Float, nullable=False, default=0.0)

    status = Column(String, nullable=False, index=True)
    alert  = Column(Boolean, nullable=False, default=False)

    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc),
                        onupdate=lambda: datetime.now(timezone.utc), nullable=False)

    __table_args__ = (
        Index('idx_quantity_date_costcode', 'tracking_date', 'cost_code_id'),
        Index('idx_quantity_date_job',      'tracking_date', 'job_id'),
        Index('idx_quantity_status',        'status'),
        Index('idx_quantity_cost_type',     'cost_type'),
    )


# ===========================================================================
# QUANTITY TEST (isolated daily-sum experiment — does NOT affect production)
# ===========================================================================

class QuantityTest(Base):
    """
    Daily quantity rows for the proposed daily-sum read model.

    Populated only by scripts/populate_quantity_test.py.
    Read only by /api/quantity-test/* routes.
    Never written by production cron or /api/quantity/* routes.
    """
    __tablename__ = "quantity_test"

    id            = Column(String, primary_key=True, index=True)
    tracking_date = Column(String, nullable=False, index=True)

    cost_code_id = Column(String, nullable=False, index=True)
    cost_code    = Column(String, nullable=False)
    description  = Column(String, nullable=False)
    job_id       = Column(String, nullable=False, index=True)
    job_code     = Column(String, nullable=False)
    unit         = Column(String, nullable=False)
    cost_type    = Column(String, nullable=False, index=True)

    planned_quantity   = Column(Float, nullable=False, default=0.0)
    installed_quantity = Column(Float, nullable=False, default=0.0)
    remaining_quantity = Column(Float, nullable=False, default=0.0)
    percent_complete   = Column(Float, nullable=False, default=0.0)

    status = Column(String, nullable=False, index=True)
    alert  = Column(Boolean, nullable=False, default=False)

    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc),
                        onupdate=lambda: datetime.now(timezone.utc), nullable=False)

    __table_args__ = (
        Index('idx_quantity_test_date', 'tracking_date'),
        Index('idx_quantity_test_date_costcode', 'tracking_date', 'cost_code_id'),
        Index('idx_quantity_test_date_job', 'tracking_date', 'job_id'),
    )


# ===========================================================================
# BUDGET TRACKING MODELS
# ===========================================================================

class BudgetTrackingCache(Base):
    __tablename__ = "budget_tracking_cache"

    id            = Column(String, primary_key=True, index=True)
    tracking_date = Column(String, nullable=False, index=True)

    total_cost_codes = Column(Integer, nullable=False, default=0)
    on_track_count   = Column(Integer, nullable=False, default=0)
    over_risk_count  = Column(Integer, nullable=False, default=0)

    fetched_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc),
                        onupdate=lambda: datetime.now(timezone.utc), nullable=False)

    __table_args__ = (
        Index('idx_budget_date', 'tracking_date', unique=True),
    )


class BudgetTrackingResult(Base):
    __tablename__ = "budget_tracking_results"

    id            = Column(String, primary_key=True, index=True)
    tracking_date = Column(String, nullable=False, index=True)

    cost_code_id          = Column(String, nullable=False, index=True)
    cost_code             = Column(String, nullable=False)
    cost_code_description = Column(String, nullable=False)
    job_id                = Column(String, nullable=False, index=True)
    job_name              = Column(String, nullable=False)
    business_unit         = Column(String, nullable=True)

    budgeted_all_cost      = Column(Float, nullable=False, default=0.0)
    actual_cost            = Column(Float, nullable=False, default=0.0)
    utilization_percentage = Column(Integer, nullable=True, default=None)
    variance               = Column(Float, nullable=False, default=0.0)
    status                 = Column(String, nullable=False, index=True)

    # ── Actual cost breakdown ─────────────────────────────────────────
    labor_cost        = Column(Float, nullable=False, default=0.0)
    equipment_cost    = Column(Float, nullable=False, default=0.0)
    material_cost     = Column(Float, nullable=False, default=0.0)
    subcontract_cost  = Column(Float, nullable=False, default=0.0)
    trucking_cost     = Column(Float, nullable=False, default=0.0)
    labor_hours       = Column(Float, nullable=False, default=0.0)
    quantity          = Column(Float, nullable=False, default=0.0)

    # ── Planned budget breakdown ──────────────────────────────────────
    labor_budget        = Column(Float, nullable=False, default=0.0)
    equipment_budget    = Column(Float, nullable=False, default=0.0)
    material_budget     = Column(Float, nullable=False, default=0.0)
    subcontract_budget  = Column(Float, nullable=False, default=0.0)

    # ── Planned quantity from v1/costCodes (info-only, not used in calculations) ──
    # Sourced directly from the HCSS cost code budget record — same API call
    # that fetches dollar budgets (Call 2 in BudgetHCSSClient).
    # Stored here for verification purposes. Not used for expected_budget math.
    planned_quantity    = Column(Float, nullable=False, default=0.0)
    unit                = Column(String, nullable=False, default="")

    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc),
                        onupdate=lambda: datetime.now(timezone.utc), nullable=False)

    __table_args__ = (
        Index('idx_budget_date_costcode', 'tracking_date', 'cost_code_id'),
        Index('idx_budget_date_job',      'tracking_date', 'job_id'),
        Index('idx_budget_status',        'status'),
    )


# ===========================================================================
# PROJECTION TRACKING MODELS   ← NEW — added for Projection Tracking module
# ===========================================================================

class ProjectionTrackingCache(Base):                                    # ← NEW
    """
    Cache table for monthly projection results.
    One row per tracking_month (YYYY-MM).

    Key difference from budget/quantity caches:
      - tracking_month is YYYY-MM not YYYY-MM-DD
      - Projections are calculated monthly, not daily
      - Has at_risk_count and over_budget_count (3 statuses, not 2)

    Added as part of Projection Tracking module.
    """
    __tablename__ = "projection_tracking_cache"

    id             = Column(String, primary_key=True, index=True)
    tracking_month = Column(String, nullable=False, index=True)   # YYYY-MM

    # Summary counts
    total_jobs        = Column(Integer, nullable=False, default=0)
    on_track_count    = Column(Integer, nullable=False, default=0)
    at_risk_count     = Column(Integer, nullable=False, default=0)
    over_budget_count = Column(Integer, nullable=False, default=0)
    alerts_count      = Column(Integer, nullable=False, default=0)

    # Metadata
    fetched_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc),
                        onupdate=lambda: datetime.now(timezone.utc), nullable=False)

    __table_args__ = (
        Index('idx_projection_month', 'tracking_month', unique=True),
    )


class ProjectionTrackingResult(Base):                                   # ← NEW
    """
    Per-job projection result for a given month.
    Linked to ProjectionTrackingCache via tracking_month.

    Stores all fields needed by the frontend and email report:
      - Contract values (original + change orders)
      - EAC (projected_final_cost)
      - Cost variance
      - Billing position
      - Status flags

    Added as part of Projection Tracking module.
    """
    __tablename__ = "projection_tracking_results"

    id             = Column(String, primary_key=True, index=True)
    tracking_month = Column(String, nullable=False, index=True)   # YYYY-MM

    # Job identification
    job_id        = Column(String, nullable=False, index=True)
    job_code      = Column(String, nullable=False)
    job_name      = Column(String, nullable=False)
    business_unit = Column(String, nullable=True)



# Production Analysis / cost-code identification
    period_start = Column(String, nullable=True)
    period_end   = Column(String, nullable=True)

    cost_code_id          = Column(String, nullable=False, default="", index=True)
    cost_code             = Column(String, nullable=False, default="")
    cost_code_description = Column(String, nullable=False, default="")
    unit                  = Column(String, nullable=False, default="")

    # Quantity / completion
    budgeted_quantity = Column(Float, nullable=False, default=0.0)
    quantity          = Column(Float, nullable=False, default=0.0)
    completion_pct    = Column(Float, nullable=False, default=0.0)

    # Production Analysis cost fields
    budgeted_cost = Column(Float, nullable=False, default=0.0)
    expected      = Column(Float, nullable=False, default=0.0)
    actual        = Column(Float, nullable=False, default=0.0)
    variance      = Column(Float, nullable=False, default=0.0)

    # Projection fields
    projected_final       = Column(Float, nullable=False, default=0.0)
    projected_over_under  = Column(Float, nullable=False, default=0.0)
    performance_factor    = Column(Float, nullable=False, default=0.0)

    # Actual cost breakdown
    actual_labor_cost       = Column(Float, nullable=False, default=0.0)
    actual_equipment_cost   = Column(Float, nullable=False, default=0.0)
    actual_material_cost    = Column(Float, nullable=False, default=0.0)
    actual_subcontract_cost = Column(Float, nullable=False, default=0.0)
    actual_trucking_cost    = Column(Float, nullable=False, default=0.0)

    # Supporting HCSS values
    quantity_from_job_costs = Column(Float, nullable=False, default=0.0)
    labor_hours            = Column(Float, nullable=False, default=0.0)
    equipment_hours        = Column(Float, nullable=False, default=0.0)



    # Contract
    original_contract_value = Column(Float, nullable=False, default=0.0)
    approved_change_orders  = Column(Float, nullable=False, default=0.0)
    revised_contract_value  = Column(Float, nullable=False, default=0.0)

    # Cost / EAC
    original_budget      = Column(Float, nullable=False, default=0.0)
    actual_cost_to_date  = Column(Float, nullable=False, default=0.0)
    hcss_forecast_cost   = Column(Float, nullable=False, default=0.0)   # ← NEW raw HCSS forecast
    projected_final_cost = Column(Float, nullable=False, default=0.0)
    cost_variance        = Column(Float, nullable=False, default=0.0)
    forecast_source      = Column(String, nullable=False, default="eac_formula")  # ← NEW

    # Progress
    percent_complete             = Column(Float,  nullable=False, default=0.0)
    estimated_completion_month   = Column(String, nullable=True)   # YYYY-MM

    # Billing
    billed_to_date          = Column(Float, nullable=False, default=0.0)
    projected_final_billing = Column(Float, nullable=False, default=0.0)
    billing_variance        = Column(Float, nullable=False, default=0.0)

    # Flags
    status           = Column(String,  nullable=False, index=True)  # ON_TRACK | AT_RISK | OVER_BUDGET
    alert            = Column(Boolean, nullable=False, default=False)
    discrepancy_flag = Column(Boolean, nullable=False, default=False)

    # Metadata
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc),
                        onupdate=lambda: datetime.now(timezone.utc), nullable=False)

    __table_args__ = (
        Index('idx_projection_month_job',   'tracking_month', 'job_id'),
        Index('idx_projection_month_costcode', 'tracking_month', 'cost_code_id'),
        Index('idx_projection_status',      'status'),
        Index('idx_projection_alert',       'alert'),
        Index('idx_projection_discrepancy', 'discrepancy_flag'),
    )


# ===========================================================================
# CRON JOB TRACKING MODEL
# ===========================================================================

class CronJobExecution(Base):
    __tablename__ = "cron_job_executions"

    id             = Column(String, primary_key=True, index=True)
    job_type       = Column(String, nullable=False, index=True)
    execution_date = Column(String, nullable=False, index=True)

    status        = Column(String,  nullable=False, index=True)
    attempt_count = Column(Integer, nullable=False, default=1)
    max_retries   = Column(Integer, nullable=False, default=3)

    records_processed = Column(Integer, nullable=True)
    error_message     = Column(Text,    nullable=True)

    started_at   = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)
    next_retry_at = Column(DateTime, nullable=True)
    created_at   = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
    updated_at   = Column(DateTime, default=lambda: datetime.now(timezone.utc),
                          onupdate=lambda: datetime.now(timezone.utc), nullable=False)

    __table_args__ = (
        Index('idx_cron_job_type_date', 'job_type', 'execution_date'),
        Index('idx_cron_status',        'status'),
        Index('idx_cron_next_retry',    'next_retry_at'),
    )


# ===========================================================================
# SYNC STATE (key/value cursors for incremental HCSS sync)
# ===========================================================================

class SyncState(Base):
    """
    Generic key/value store for sync cursors (e.g. budget_last_sync_at).
    Used so cron can discover historically-dated HCSS edits via modifiedSince
    without re-fetching the entire history every run.
    """
    __tablename__ = "sync_state"

    key        = Column(String, primary_key=True)
    value      = Column(Text, nullable=False)
    updated_at = Column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )


# ===========================================================================
# SCHEDULE MONITORING MODELS
# ===========================================================================

class ScheduleMonitoringCache(Base):
    """
    One row per tracking week (week_end_date YYYY-MM-DD).
    Summary counts for the weekly schedule monitoring run.
    """
    __tablename__ = "schedule_monitoring_cache"

    id             = Column(String, primary_key=True, index=True)
    week_end_date  = Column(String, nullable=False, index=True)   # YYYY-MM-DD (Friday)
    period_start   = Column(String, nullable=False)
    period_end     = Column(String, nullable=False)

    total_jobs          = Column(Integer, nullable=False, default=0)
    jobs_with_critical  = Column(Integer, nullable=False, default=0)
    jobs_with_warnings  = Column(Integer, nullable=False, default=0)
    total_alerts        = Column(Integer, nullable=False, default=0)
    critical_count      = Column(Integer, nullable=False, default=0)
    warning_count       = Column(Integer, nullable=False, default=0)

    fetched_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc),
                        onupdate=lambda: datetime.now(timezone.utc), nullable=False)

    __table_args__ = (
        Index('idx_schedule_week', 'week_end_date', unique=True),
    )


class ScheduleMonitoringAlert(Base):
    """
    One row per alert generated by the schedule monitoring engine.
    Linked to ScheduleMonitoringCache via week_end_date.
    """
    __tablename__ = "schedule_monitoring_alerts"

    id            = Column(String, primary_key=True, index=True)
    week_end_date = Column(String, nullable=False, index=True)

    job_id        = Column(String, nullable=False, index=True)
    job_code      = Column(String, nullable=False)
    job_name      = Column(String, nullable=False)
    business_unit = Column(String, nullable=True)

    severity      = Column(String, nullable=False, index=True)   # "critical" | "warning"
    alert_type    = Column(String, nullable=False, index=True)
    message       = Column(String, nullable=False)
    cost_code_id  = Column(String, nullable=True)
    cost_code_name = Column(String, nullable=True)
    detail_json   = Column(Text, nullable=False, default="{}")
    generated_at  = Column(String, nullable=False)

    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)

    __table_args__ = (
        Index('idx_schedule_alert_week',     'week_end_date'),
        Index('idx_schedule_alert_job',      'job_id'),
        Index('idx_schedule_alert_severity', 'severity'),
        Index('idx_schedule_alert_type',     'alert_type'),
    )


class ScheduleMonitoringJobSummary(Base):
    """
    One row per job per week.  Stores the rolled-up job-level status
    and key metrics so the API can return summaries without re-running analysis.
    """
    __tablename__ = "schedule_monitoring_job_summaries"

    id            = Column(String, primary_key=True, index=True)
    week_end_date = Column(String, nullable=False, index=True)

    job_id        = Column(String, nullable=False, index=True)
    job_code      = Column(String, nullable=False)
    job_name      = Column(String, nullable=False)
    business_unit = Column(String, nullable=True)

    status            = Column(String, nullable=False, index=True)  # ON_TRACK|WARNING|CRITICAL
    critical_alerts   = Column(Integer, nullable=False, default=0)
    warning_alerts    = Column(Integer, nullable=False, default=0)

    cost_pct          = Column(Float, nullable=False, default=0.0)
    quantity_pct      = Column(Float, nullable=False, default=0.0)
    total_budget_cost = Column(Float, nullable=False, default=0.0)
    total_actual_cost = Column(Float, nullable=False, default=0.0)
    total_labor_hours = Column(Float, nullable=False, default=0.0)

    days_since_activity  = Column(Integer, nullable=False, default=0)
    last_timecard_date   = Column(String, nullable=True)
    estimated_end_date   = Column(String, nullable=True)

    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)

    __table_args__ = (
        Index('idx_schedule_summary_week',   'week_end_date'),
        Index('idx_schedule_summary_job',    'job_id'),
        Index('idx_schedule_summary_status', 'status'),
    )


# ===========================================================================
# BILLING DRAFT (monthly) — one flat row per (period, job_code, cost_code).
# Mirrors the Budget grid: query by (period, job_code) returns all the job's
# cost-code rows. Values are aggregated over the month from the SAME budget +
# quantity data the Budget module uses, so they match HCSS exactly.
# ===========================================================================

class BillingDraft(Base):
    __tablename__ = "billing_draft"

    id     = Column(String, primary_key=True, index=True)
    period = Column(String, nullable=False, index=True)   # 'YYYY-MM' (the billing month)

    job_id          = Column(String, nullable=True, index=True)
    job_code        = Column(String, nullable=False, index=True)
    job_description = Column(String, nullable=True)

    cost_code_id = Column(String, nullable=True)
    cost_code    = Column(String, nullable=False)
    description  = Column(String, nullable=True)
    unit         = Column(String, nullable=True)

    budgeted_quantity = Column(Float, nullable=False, default=0.0)   # planned qty
    actual_quantity   = Column(Float, nullable=False, default=0.0)   # installed/actual qty
    percent_complete  = Column(Float, nullable=False, default=0.0)

    budgeted_all_cost        = Column(Float, nullable=False, default=0.0)  # HCSS full budget
    expected_all_cost        = Column(Float, nullable=False, default=0.0)  # (actual/planned)*budget
    capped_expected_all_cost = Column(Float, nullable=False, default=0.0)  # min(expected, budgeted)
    actual_all_cost          = Column(Float, nullable=False, default=0.0)
    variance                 = Column(Float, nullable=False, default=0.0)  # expected - actual
    capped_variance          = Column(Float, nullable=False, default=0.0)  # capped - actual

    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc),
                        onupdate=lambda: datetime.now(timezone.utc), nullable=False)

    __table_args__ = (
        Index('idx_billing_draft_period_job', 'period', 'job_code'),
        Index('idx_billing_draft_period',     'period'),
    )


class BillingDraftAIA(Base):
    """One AIA G702 draft application per (period, job_code).

    Summary fields are columns (queryable); line_items / assumptions / model meta
    are stored as JSON text. Generated by Gemini (judgment) + Python (dollar math).
    """
    __tablename__ = "billing_draft_aia"

    id     = Column(String, primary_key=True, index=True)
    period = Column(String, nullable=False, index=True)   # 'YYYY-MM'

    job_id          = Column(String, nullable=True)
    job_code        = Column(String, nullable=False, index=True)
    job_description = Column(String, nullable=True)
    application_no  = Column(String, nullable=True)
    period_to       = Column(String, nullable=True)
    prepared_date   = Column(String, nullable=True)

    original_contract_sum          = Column(Float, nullable=False, default=0.0)
    net_change_by_co               = Column(Float, nullable=False, default=0.0)
    contract_sum_to_date           = Column(Float, nullable=False, default=0.0)
    total_completed_stored_to_date = Column(Float, nullable=False, default=0.0)
    retainage_pct                  = Column(Float, nullable=False, default=0.0)
    total_retainage                = Column(Float, nullable=False, default=0.0)
    total_earned_less_retainage    = Column(Float, nullable=False, default=0.0)
    less_previous_certificates     = Column(Float, nullable=False, default=0.0)
    current_payment_due            = Column(Float, nullable=False, default=0.0)
    gross_completed_this_period    = Column(Float, nullable=False, default=0.0)
    remaining_contract_balance     = Column(Float, nullable=False, default=0.0)
    items_billed                   = Column(Integer, nullable=False, default=0)
    items_held                     = Column(Integer, nullable=False, default=0)

    assumptions = Column(Text, nullable=True, default="[]")   # JSON list[str]
    line_items  = Column(Text, nullable=True, default="[]")   # JSON list[dict]
    model_meta  = Column(Text, nullable=True, default="{}")   # JSON dict
# POSTMORTEM TRACKING MODELS
# ===========================================================================

class PostmortemJobSnapshot(Base):
    """
    One row per job — job-level postmortem totals.

    Populated by the postmortem cron job using the same formula as
    get_job_expected_all_cost.py (verified against HCSS portal):

        T&M cost codes        : expected = actual
        Normal with qty/budget: expected = min((actualQty / budgetQty) * budget, budget)
        No qty or budget      : expected = 0

    This is the authoritative source for postmortem job-level KPIs.
    """
    __tablename__ = "postmortem_job_snapshots"

    job_id        = Column(String, primary_key=True, index=True)   # HCSS job UUID
    job_code      = Column(String, nullable=False)
    job_name      = Column(String, nullable=False)
    business_unit = Column(String, nullable=True)

    # Job-level aggregated totals
    budgeted_all_cost      = Column(Float, nullable=False, default=0.0)
    expected_all_cost      = Column(Float, nullable=False, default=0.0)
    actual_cost            = Column(Float, nullable=False, default=0.0)
    variance               = Column(Float, nullable=False, default=0.0)   # expected - actual
    utilization_pct        = Column(Float, nullable=True)                 # actual/expected*100
    capped_cost_code_count = Column(Integer, nullable=False, default=0)   # codes where qty>100%

    fetched_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc),
                        onupdate=lambda: datetime.now(timezone.utc), nullable=False)

    # Relationship to cost code rows
    cost_codes = relationship(
        "PostmortemCostCodeResult",
        back_populates="job",
        cascade="all, delete-orphan",
        lazy="dynamic",
    )

    __table_args__ = (
        Index("idx_postmortem_job_code",   "job_code"),
        Index("idx_postmortem_fetched_at", "fetched_at"),
    )


class PostmortemCostCodeResult(Base):
    """
    One row per cost code per job — full per-cost-code postmortem detail.

    Stores all fields produced by get_job_expected_all_cost.py so the API
    can serve complete postmortem detail without re-fetching HCSS.

    calc_method values:
        "T&M (expected = actual)"   — T&M cost code
        "formula"                   — normal, qty < 100%
        "cap-at-budget (qty > 100%)"— capped because qty exceeded budget
        "zero (no qty/budget)"      — no budget or quantity on record
        "unbudgeted"                — cost exists but no budget row
    """
    __tablename__ = "postmortem_cost_code_results"

    id            = Column(String, primary_key=True, index=True)
    job_id        = Column(String, ForeignKey("postmortem_job_snapshots.job_id",
                                              ondelete="CASCADE"),
                           nullable=False, index=True)

    cost_code_id  = Column(String, nullable=False, index=True)
    cost_code     = Column(String, nullable=False)
    description   = Column(String, nullable=False, default="")

    # HCSS flags from v1/costCodes
    is_tm           = Column(Boolean, nullable=False, default=False)
    is_cap_expected = Column(Boolean, nullable=False, default=True)

    # Quantities
    budgeted_qty   = Column(Float, nullable=False, default=0.0)
    actual_qty     = Column(Float, nullable=False, default=0.0)
    completion_pct = Column(Float, nullable=True)     # null when budgeted_qty == 0

    # Dollar values
    budgeted_all_cost  = Column(Float, nullable=False, default=0.0)
    raw_formula        = Column(Float, nullable=True)   # uncapped formula result
    expected_all_cost  = Column(Float, nullable=False, default=0.0)
    actual_cost        = Column(Float, nullable=False, default=0.0)
    variance           = Column(Float, nullable=False, default=0.0)  # expected - actual

    # Derived
    calc_method    = Column(String, nullable=False, default="")
    utilization_pct = Column(Float, nullable=True)   # actual/expected*100
    loss_amount    = Column(Float, nullable=False, default=0.0)  # max(0, actual-expected)
    loss_pct       = Column(Float, nullable=True)

    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc),
                        onupdate=lambda: datetime.now(timezone.utc), nullable=False)

    __table_args__ = (
        Index('idx_billing_aia_period_job', 'period', 'job_code'),
    )
    # Relationship back to job
    job = relationship("PostmortemJobSnapshot", back_populates="cost_codes")

    __table_args__ = (
        Index("idx_postmortem_cc_job",       "job_id"),
        Index("idx_postmortem_cc_ccid",      "cost_code_id"),
        Index("idx_postmortem_cc_job_ccid",  "job_id", "cost_code_id"),
    )


def create_tables() -> None:
    """Create all tables if they don't exist yet.
    Projection + Schedule + Postmortem tables are created automatically
    because the models are registered with Base above.
    """
    Base.metadata.create_all(bind=engine)
    _run_lightweight_migrations()
    _cleanup_quantity_period_keys_once()


def _cleanup_quantity_period_keys_once() -> None:
    """Idempotent removal of legacy quantity start__end period snapshot keys."""
    try:
        from api.db_services import cleanup_quantity_period_cache_keys
        db = SessionLocal()
        try:
            cleanup_quantity_period_cache_keys(db)
        finally:
            db.close()
    except Exception:
        logger.exception(
            "Quantity period-key cleanup failed (continuing; cron will retry)"
        )


def _run_lightweight_migrations() -> None:
    """Idempotent ALTER TABLEs for columns added after a table already exists.
    `create_all` only creates missing tables, not new columns on existing ones,
    so newly added columns are applied here (safe to run on every startup).
    """
    from sqlalchemy import text

    statements = [
        "ALTER TABLE timelog_timecard_results ADD COLUMN IF NOT EXISTS job_description VARCHAR",
        "ALTER TABLE timelog_timecard_results ADD COLUMN IF NOT EXISTS issues TEXT DEFAULT '[]'",
        "ALTER TABLE timelog_timecard_results ADD COLUMN IF NOT EXISTS portal_sync_status VARCHAR DEFAULT 'pending'",
        "ALTER TABLE timelog_timecard_results ADD COLUMN IF NOT EXISTS portal_synced_at TIMESTAMP",
    ]
    try:
        with engine.begin() as conn:
            for stmt in statements:
                conn.execute(text(stmt))
        logger.info("Lightweight migrations applied (timelog_timecard_results columns)")
    except Exception:
        logger.exception("Lightweight migrations failed (continuing; columns may already exist)")


def get_db():
    """FastAPI dependency — yields a DB session and closes it after the request."""
    db: Session = SessionLocal()
    try:
        yield db
    finally:
        db.close()