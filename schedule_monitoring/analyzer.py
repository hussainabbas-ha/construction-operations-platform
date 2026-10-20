"""
schedule_monitoring/analyzer.py
─────────────────────────────────
Analyzes JobSnapshot data and produces ScheduleAlert list.

Rules (no baseline needed — all run on actuals only):

CRITICAL alerts:
  C1  Cost overrun       — cost_pct >= 115% (15%+ over budget)
  C2  Schedule overrun   — estimated_end_date is in the past
  C3  Seasonal risk      — asphalt/concrete past deadline
  C4  Sub blocked        — subcontractor items exist but no quantity progress
  C5  Work stoppage      — days_since_activity >= STOPPAGE_DAYS and job is active

WARNING alerts:
  W1  Cost threshold     — cost_pct >= 75%
  W2  Quantity threshold — quantity_pct >= 75%
  W3  Near deadline      — estimated_end_date within WARNING_DAYS_OUT days
  W4  Quantity/cost mismatch — cost_pct >> quantity_pct by MISMATCH_THRESHOLD
  W5  No labor hours     — has timelog activity but labor_hours == 0 for cost code
  W6  Sub no txn         — subcontractor items exist but zero subcontract cost

When baseline arrives (Google Drive 6-week schedule), additional checks plug in:
  - Planned vs actual quantity per cost code per week
  - Planned vs actual start date per task
  - Out-of-sequence work detection on critical path
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Literal

from schedule_monitoring.data_gatherer import CostCodeSnapshot, JobSnapshot

logger = logging.getLogger(__name__)

# ── Thresholds ────────────────────────────────────────────────────────────────
COST_WARNING_PCT       = 75.0     # W1
COST_CRITICAL_PCT      = 115.0    # C1  (15% loss)
QUANTITY_WARNING_PCT   = 75.0     # W2
WARNING_DAYS_OUT       = 14       # W3  days until estimated completion
STOPPAGE_DAYS          = 5        # C5  consecutive calendar days no timecards
MISMATCH_THRESHOLD     = 25.0     # W4  cost_pct - quantity_pct delta


Severity = Literal["critical", "warning"]
AlertType = Literal[
    "cost_overrun",
    "cost_threshold",
    "quantity_threshold",
    "schedule_overrun",
    "schedule_warning",
    "seasonal_risk",
    "sub_blocked",
    "sub_no_transaction",
    "work_stoppage",
    "qty_cost_mismatch",
    "no_labor_hours",
]


@dataclass(frozen=True)
class ScheduleAlert:
    job_id: str
    job_code: str
    job_name: str
    severity: Severity
    alert_type: AlertType
    message: str
    cost_code_id: str | None = None
    cost_code_name: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)
    generated_at: str = field(default_factory=lambda: datetime.now().isoformat())


@dataclass
class ScheduleReport:
    """Full analysis result for a period."""
    period_start: str
    period_end: str
    generated_at: str
    total_jobs: int
    jobs_with_critical: int
    jobs_with_warnings: int
    total_alerts: int
    critical_count: int
    warning_count: int
    alerts: list[ScheduleAlert]
    job_summaries: list[dict[str, Any]]   # one dict per job with rolled-up status


class ScheduleAnalyzer:
    """
    Runs all alert rules against a list of JobSnapshot objects.
    Returns a ScheduleReport.
    """

    def __init__(
        self,
        cost_warning_pct: float = COST_WARNING_PCT,
        cost_critical_pct: float = COST_CRITICAL_PCT,
        quantity_warning_pct: float = QUANTITY_WARNING_PCT,
        warning_days_out: int = WARNING_DAYS_OUT,
        stoppage_days: int = STOPPAGE_DAYS,
        mismatch_threshold: float = MISMATCH_THRESHOLD,
    ) -> None:
        self.cost_warning_pct    = cost_warning_pct
        self.cost_critical_pct   = cost_critical_pct
        self.quantity_warning_pct = quantity_warning_pct
        self.warning_days_out    = warning_days_out
        self.stoppage_days       = stoppage_days
        self.mismatch_threshold  = mismatch_threshold

    def analyze(
        self,
        snapshots: list[JobSnapshot],
        period_start: str,
        period_end: str,
    ) -> ScheduleReport:
        """Run all rules and return a ScheduleReport."""
        all_alerts: list[ScheduleAlert] = []

        for job in snapshots:
            job_alerts = self._analyze_job(job)
            all_alerts.extend(job_alerts)

        critical = [a for a in all_alerts if a.severity == "critical"]
        warnings = [a for a in all_alerts if a.severity == "warning"]

        # Job-level summaries
        job_alert_map: dict[str, list[ScheduleAlert]] = {}
        for a in all_alerts:
            job_alert_map.setdefault(a.job_id, []).append(a)

        job_summaries: list[dict[str, Any]] = []
        for job in snapshots:
            jalerts = job_alert_map.get(job.job_id, [])
            n_crit  = sum(1 for a in jalerts if a.severity == "critical")
            n_warn  = sum(1 for a in jalerts if a.severity == "warning")
            if n_crit:
                status = "CRITICAL"
            elif n_warn:
                status = "WARNING"
            else:
                status = "ON_TRACK"

            job_summaries.append({
                "job_id": job.job_id,
                "job_code": job.job_code,
                "job_name": job.job_name,
                "business_unit": job.business_unit,
                "status": status,
                "critical_alerts": n_crit,
                "warning_alerts": n_warn,
                "cost_pct": job.cost_pct,
                "quantity_pct": job.quantity_pct,
                "total_budget_cost": job.total_budget_cost,
                "total_actual_cost": job.total_actual_cost,
                "total_labor_hours": job.total_labor_hours,
                "days_since_activity": job.days_since_activity,
                "last_timecard_date": job.last_timecard_date,
                "estimated_end_date": job.estimated_end_date,
            })

        jobs_with_critical = sum(
            1 for j in job_summaries if j["critical_alerts"] > 0
        )
        jobs_with_warnings = sum(
            1 for j in job_summaries if j["warning_alerts"] > 0
        )

        logger.info(
            "Schedule analysis: %d jobs | %d critical | %d warning | period=%s→%s",
            len(snapshots), len(critical), len(warnings),
            period_start, period_end,
        )

        return ScheduleReport(
            period_start=period_start,
            period_end=period_end,
            generated_at=datetime.now().isoformat(),
            total_jobs=len(snapshots),
            jobs_with_critical=jobs_with_critical,
            jobs_with_warnings=jobs_with_warnings,
            total_alerts=len(all_alerts),
            critical_count=len(critical),
            warning_count=len(warnings),
            alerts=all_alerts,
            job_summaries=job_summaries,
        )

    # ── Per-job rule runner ───────────────────────────────────────────────────

    def _analyze_job(self, job: JobSnapshot) -> list[ScheduleAlert]:
        alerts: list[ScheduleAlert] = []
        today = date.today()

        # C5 — Work stoppage
        if job.days_since_activity >= self.stoppage_days and job.active_days > 0:
            alerts.append(ScheduleAlert(
                job_id=job.job_id, job_code=job.job_code, job_name=job.job_name,
                severity="critical", alert_type="work_stoppage",
                message=(
                    f"No timecard activity for {job.days_since_activity} days "
                    f"(last: {job.last_timecard_date}). Possible work stoppage."
                ),
                detail={"days_since_activity": job.days_since_activity,
                        "last_timecard_date": job.last_timecard_date},
            ))

        # C2 — Schedule overrun (est. completion in the past)
        if job.estimated_end_date:
            try:
                est_end = datetime.fromisoformat(job.estimated_end_date).date()
                overrun_days = (today - est_end).days
                if overrun_days > 0:
                    alerts.append(ScheduleAlert(
                        job_id=job.job_id, job_code=job.job_code, job_name=job.job_name,
                        severity="critical", alert_type="schedule_overrun",
                        message=(
                            f"Job is {overrun_days} day(s) past estimated completion "
                            f"({job.estimated_end_date})."
                        ),
                        detail={"estimated_end_date": job.estimated_end_date,
                                "days_overrun": overrun_days},
                    ))
                elif overrun_days >= -self.warning_days_out:
                    remaining = abs(overrun_days)
                    alerts.append(ScheduleAlert(
                        job_id=job.job_id, job_code=job.job_code, job_name=job.job_name,
                        severity="warning", alert_type="schedule_warning",
                        message=(
                            f"Job has {remaining} day(s) until estimated completion "
                            f"({job.estimated_end_date})."
                        ),
                        detail={"estimated_end_date": job.estimated_end_date,
                                "days_remaining": remaining},
                    ))
            except (ValueError, AttributeError):
                pass

        # Per-cost-code rules
        for cc in job.cost_codes:
            alerts.extend(self._analyze_cost_code(job, cc, today))

        # Sub-level checks (job-level)
        if job.subcontractor_items:
            sub_cost = sum(cc.subcontract_cost for cc in job.cost_codes)
            qty_progress = sum(
                1 for cc in job.cost_codes
                if cc.cost_type == "subcontractor" and cc.installed_quantity > 0
            )
            if qty_progress == 0 and sub_cost == 0:
                alerts.append(ScheduleAlert(
                    job_id=job.job_id, job_code=job.job_code, job_name=job.job_name,
                    severity="critical", alert_type="sub_blocked",
                    message=(
                        f"Job has {len(job.subcontractor_items)} subcontract item(s) "
                        f"but no subcontractor quantity or cost recorded in this period."
                    ),
                    detail={"sub_item_count": len(job.subcontractor_items)},
                ))
            elif sub_cost == 0 and qty_progress > 0:
                alerts.append(ScheduleAlert(
                    job_id=job.job_id, job_code=job.job_code, job_name=job.job_name,
                    severity="warning", alert_type="sub_no_transaction",
                    message=(
                        "Subcontractor quantity logged but $0 subcontract cost recorded. "
                        "Billing transaction may be missing."
                    ),
                    detail={"sub_cost": sub_cost, "qty_progress_ccs": qty_progress},
                ))

        return alerts

    # ── Per-cost-code rules ───────────────────────────────────────────────────

    def _analyze_cost_code(
        self,
        job: JobSnapshot,
        cc: CostCodeSnapshot,
        today: date,
    ) -> list[ScheduleAlert]:
        alerts: list[ScheduleAlert] = []

        def _alert(severity: Severity, alert_type: AlertType, message: str, **detail):
            return ScheduleAlert(
                job_id=job.job_id, job_code=job.job_code, job_name=job.job_name,
                severity=severity, alert_type=alert_type, message=message,
                cost_code_id=cc.cost_code_id, cost_code_name=cc.cost_code,
                detail=dict(detail),
            )

        # C1 — Cost overrun (>= 15% loss)
        if cc.budget_cost > 0 and cc.cost_pct >= self.cost_critical_pct:
            loss_pct = cc.cost_pct - 100
            alerts.append(_alert(
                "critical", "cost_overrun",
                f"Cost code '{cc.cost_code}' – {cc.description}: "
                f"{loss_pct:.1f}% loss "
                f"(${cc.actual_cost:,.2f} actual vs ${cc.budget_cost:,.2f} budget).",
                cost_pct=cc.cost_pct, loss_pct=round(loss_pct, 2),
                actual_cost=cc.actual_cost, budget_cost=cc.budget_cost,
            ))
        # W1 — Cost threshold (>= 75%)
        elif cc.budget_cost > 0 and cc.cost_pct >= self.cost_warning_pct:
            alerts.append(_alert(
                "warning", "cost_threshold",
                f"Cost code '{cc.cost_code}' – {cc.description}: "
                f"{cc.cost_pct:.1f}% of budget used "
                f"(${cc.actual_cost:,.2f} / ${cc.budget_cost:,.2f}).",
                cost_pct=cc.cost_pct,
                actual_cost=cc.actual_cost, budget_cost=cc.budget_cost,
            ))

        # W2 — Quantity threshold (>= 75%)
        if cc.planned_quantity > 0 and cc.quantity_pct >= self.quantity_warning_pct:
            alerts.append(_alert(
                "warning", "quantity_threshold",
                f"Cost code '{cc.cost_code}' – {cc.description}: "
                f"{cc.quantity_pct:.1f}% of planned quantity installed "
                f"({cc.installed_quantity:.2f} / {cc.planned_quantity:.2f} {cc.unit}).",
                quantity_pct=cc.quantity_pct,
                installed=cc.installed_quantity, planned=cc.planned_quantity,
                unit=cc.unit,
            ))

        # C3 — Seasonal risk
        if cc.seasonal_risk:
            deadline = "September 30" if cc.seasonal_risk == "asphalt" else "October 15"
            alerts.append(_alert(
                "critical", "seasonal_risk",
                f"{cc.seasonal_risk.capitalize()} work '{cc.cost_code}' – {cc.description}: "
                f"only {cc.quantity_pct:.1f}% complete past {deadline} seasonal deadline.",
                seasonal_risk=cc.seasonal_risk, quantity_pct=cc.quantity_pct,
            ))

        # W4 — Quantity/cost mismatch
        if (
            cc.planned_quantity > 0
            and cc.budget_cost > 0
            and (cc.cost_pct - cc.quantity_pct) >= self.mismatch_threshold
        ):
            alerts.append(_alert(
                "warning", "qty_cost_mismatch",
                f"Cost code '{cc.cost_code}' – {cc.description}: "
                f"cost is {cc.cost_pct:.1f}% spent but only "
                f"{cc.quantity_pct:.1f}% of quantity installed — "
                f"{(cc.cost_pct - cc.quantity_pct):.1f}pt gap.",
                cost_pct=cc.cost_pct, quantity_pct=cc.quantity_pct,
                gap=round(cc.cost_pct - cc.quantity_pct, 2),
            ))

        # W5 — Labor hours missing despite activity
        if cc.has_activity and cc.actual_cost > 0 and cc.labor_hours == 0:
            alerts.append(_alert(
                "warning", "no_labor_hours",
                f"Cost code '{cc.cost_code}' – {cc.description}: "
                f"cost recorded (${cc.actual_cost:,.2f}) but 0 labor hours logged.",
                actual_cost=cc.actual_cost, labor_hours=cc.labor_hours,
            ))

        return alerts
