"""
Cron job monitoring endpoints.
Gives a full overview of cron job execution history, status, skips, refetches, and failures.
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session
from sqlalchemy import and_, desc, func

from api.database import get_db, CronJobExecution

router = APIRouter()


def _fmt(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.strftime("%Y-%m-%d %H:%M:%S UTC")


@router.get(
    "/api/cron/status",
    tags=["Cron Monitoring"],
    summary="Full cron job execution overview",
    description=(
        "Returns a complete overview of cron job execution history:\n\n"
        "- **scheduler**: Whether the background scheduler is running and next run time\n"
        "- **summary**: Total counts of success, failed, retrying jobs\n"
        "- **by_module**: Per-module breakdown (timelog, quantity, budget)\n"
        "- **recent_runs**: Last 20 job executions with status and details\n"
        "- **failed_jobs**: Any currently failed or retrying jobs\n"
        "- **coverage**: Date coverage per module (what dates have data)\n\n"
        "Use `?days=7` to filter to last N days (default: 7)."
    ),
)
def cron_status(
    days: int = Query(default=7, description="How many days back to look (default: 7)"),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    from scheduler.background_scheduler import get_scheduler_status

    since = datetime.now(timezone.utc) - timedelta(days=days)

    # ── All executions in window ─────────────────────────────────────────────
    all_jobs = db.query(CronJobExecution).filter(
        CronJobExecution.created_at >= since
    ).order_by(desc(CronJobExecution.created_at)).all()

    # ── Summary counts ───────────────────────────────────────────────────────
    total       = len(all_jobs)
    success     = sum(1 for j in all_jobs if j.status == "success")
    failed      = sum(1 for j in all_jobs if j.status == "failed")
    retrying    = sum(1 for j in all_jobs if j.status == "retrying")
    running     = sum(1 for j in all_jobs if j.status == "running")
    pending     = sum(1 for j in all_jobs if j.status == "pending")

    # ── Per-module breakdown ─────────────────────────────────────────────────
    by_module: dict[str, Any] = {}
    for module in ("timelog", "quantity", "budget"):
        module_jobs = [j for j in all_jobs if j.job_type == module]
        last_success = next(
            (j for j in module_jobs if j.status == "success"), None
        )
        by_module[module] = {
            "total_runs":       len(module_jobs),
            "success":          sum(1 for j in module_jobs if j.status == "success"),
            "failed":           sum(1 for j in module_jobs if j.status == "failed"),
            "retrying":         sum(1 for j in module_jobs if j.status == "retrying"),
            "last_success_date": last_success.execution_date if last_success else None,
            "last_success_at":   _fmt(last_success.completed_at) if last_success else None,
            "last_records":      last_success.records_processed if last_success else None,
        }

    # ── Recent runs (last 20) ────────────────────────────────────────────────
    recent_runs = []
    for j in all_jobs[:20]:
        recent_runs.append({
            "id":               j.id[:8],
            "module":           j.job_type,
            "date":             j.execution_date,
            "status":           j.status,
            "records":          j.records_processed,
            "attempt":          f"{j.attempt_count}/{j.max_retries}",
            "started_at":       _fmt(j.started_at),
            "completed_at":     _fmt(j.completed_at),
            "error":            j.error_message[:120] if j.error_message else None,
            "next_retry_at":    _fmt(j.next_retry_at),
        })

    # ── Currently failing / retrying jobs ───────────────────────────────────
    problem_jobs = []
    for j in all_jobs:
        if j.status in ("failed", "retrying"):
            problem_jobs.append({
                "id":            j.id[:8],
                "module":        j.job_type,
                "date":          j.execution_date,
                "status":        j.status,
                "attempt":       f"{j.attempt_count}/{j.max_retries}",
                "error":         j.error_message[:200] if j.error_message else None,
                "next_retry_at": _fmt(j.next_retry_at),
            })

    # ── Date coverage per module ─────────────────────────────────────────────
    from api.database import (
        TimelogVerificationCache,
        QuantityTrackingCache,
        BudgetTrackingCache,
    )
    coverage: dict[str, Any] = {}

    tl = db.query(
        func.count(TimelogVerificationCache.id),
        func.min(TimelogVerificationCache.verification_date),
        func.max(TimelogVerificationCache.verification_date),
    ).first()
    coverage["timelog"] = {
        "dates_in_db": tl[0],
        "oldest_date": tl[1],
        "newest_date": tl[2],
    }

    qt = db.query(
        func.count(QuantityTrackingCache.id),
        func.min(QuantityTrackingCache.tracking_date),
        func.max(QuantityTrackingCache.tracking_date),
    ).first()
    coverage["quantity"] = {
        "dates_in_db": qt[0],
        "oldest_date": qt[1],
        "newest_date": qt[2],
    }

    bt = db.query(
        func.count(BudgetTrackingCache.id),
        func.min(BudgetTrackingCache.tracking_date),
        func.max(BudgetTrackingCache.tracking_date),
    ).first()
    coverage["budget"] = {
        "dates_in_db": bt[0],
        "oldest_date": bt[1],
        "newest_date": bt[2],
    }

    # ── Scheduler info ───────────────────────────────────────────────────────
    scheduler_info = get_scheduler_status()

    return {
        "scheduler": scheduler_info,
        "window_days": days,
        "summary": {
            "total_jobs":   total,
            "success":      success,
            "failed":       failed,
            "retrying":     retrying,
            "running":      running,
            "pending":      pending,
            "success_rate": f"{round(success / total * 100)}%" if total else "N/A",
        },
        "by_module":    by_module,
        "coverage":     coverage,
        "recent_runs":  recent_runs,
        "problem_jobs": problem_jobs,
        "generated_at": _fmt(datetime.now(timezone.utc)),
    }


@router.get(
    "/api/cron/history",
    tags=["Cron Monitoring"],
    summary="Cron job history for a specific module and date range",
    description="Filter cron job history by module and/or date.",
)
def cron_history(
    module: str | None = Query(default=None, description="timelog | quantity | budget"),
    date: str | None = Query(default=None, description="Specific execution date YYYY-MM-DD"),
    status: str | None = Query(default=None, description="success | failed | retrying | running"),
    limit: int = Query(default=50, description="Max results (default 50)"),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    query = db.query(CronJobExecution)

    if module:
        query = query.filter(CronJobExecution.job_type == module)
    if date:
        query = query.filter(CronJobExecution.execution_date == date)
    if status:
        query = query.filter(CronJobExecution.status == status)

    jobs = query.order_by(desc(CronJobExecution.created_at)).limit(limit).all()

    return {
        "filters": {"module": module, "date": date, "status": status},
        "count": len(jobs),
        "jobs": [
            {
                "id":            j.id[:8],
                "module":        j.job_type,
                "date":          j.execution_date,
                "status":        j.status,
                "records":       j.records_processed,
                "attempt":       f"{j.attempt_count}/{j.max_retries}",
                "started_at":    _fmt(j.started_at),
                "completed_at":  _fmt(j.completed_at),
                "error":         j.error_message,
                "next_retry_at": _fmt(j.next_retry_at),
            }
            for j in jobs
        ],
    }
