"""
schedule_monitoring/engine.py
───────────────────────────────
Top-level orchestrator.  Called by:
  - the cron job (weekly Friday run)
  - the API route (on-demand)

Usage:
    from schedule_monitoring.engine import run_schedule_monitoring

    report = run_schedule_monitoring(
        week_end_date="2026-06-06",   # optional, defaults to last Friday
        db=db_session,                # optional, opens its own if None
    )
"""
from __future__ import annotations

import logging
from typing import Any

from schedule_monitoring.analyzer import ScheduleAnalyzer, ScheduleReport
from schedule_monitoring.data_gatherer import ScheduleDataGatherer

logger = logging.getLogger(__name__)


def run_schedule_monitoring(
    *,
    week_end_date: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    db=None,
) -> ScheduleReport:
    """
    Gather actuals from DB (+ HCSS for job details/subs),
    run all alert rules, and return a ScheduleReport.

    Args:
        week_end_date: YYYY-MM-DD of Friday week-end (default: last Friday).
        start_date / end_date: Use for arbitrary range instead of a weekly run.
        db: SQLAlchemy session (opens own if None).

    Returns:
        ScheduleReport with all alerts and per-job summaries.
    """
    gatherer = ScheduleDataGatherer(db=db)
    analyzer = ScheduleAnalyzer()

    if start_date and end_date:
        logger.info("Running schedule monitoring: %s → %s", start_date, end_date)
        snapshots = gatherer.gather_range(start_date, end_date)
        period_start, period_end = start_date, end_date
    else:
        from datetime import date, timedelta
        from schedule_monitoring.data_gatherer import _resolve_week_end
        end_dt   = _resolve_week_end(week_end_date)
        start_dt = end_dt - timedelta(days=6)
        period_start = start_dt.strftime("%Y-%m-%d")
        period_end   = end_dt.strftime("%Y-%m-%d")
        logger.info("Running schedule monitoring (week): %s → %s", period_start, period_end)
        snapshots = gatherer.gather_week(week_end_date)

    if not snapshots:
        logger.warning("No job data found for %s → %s", period_start, period_end)
        from schedule_monitoring.analyzer import ScheduleReport
        from datetime import datetime
        return ScheduleReport(
            period_start=period_start,
            period_end=period_end,
            generated_at=datetime.now().isoformat(),
            total_jobs=0,
            jobs_with_critical=0,
            jobs_with_warnings=0,
            total_alerts=0,
            critical_count=0,
            warning_count=0,
            alerts=[],
            job_summaries=[],
        )

    return analyzer.analyze(snapshots, period_start, period_end)
