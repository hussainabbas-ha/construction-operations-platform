"""
Schedule Monitoring Module
───────────────────────────
Monitors project schedule health using actual data from DB and HCSS.

Data sources:
  - DB (quantity_tracking_results, budget_tracking_results,
         timelog_timecard_results)  ← always fresh via cron
  - HCSS /jobs/advanced             ← job names, start/end dates
  - HCSS subcontract endpoints      ← sub items (not stored in DB)

No baseline required to run.
Baseline (6-week schedule from Google Drive) plugs in via ScheduleAnalyzer
when available — adds planned vs actual comparison and critical-path checks.

Quick usage:
    from schedule_monitoring.engine import run_schedule_monitoring
    report = run_schedule_monitoring()   # defaults to last week
"""
from schedule_monitoring.engine import run_schedule_monitoring
from schedule_monitoring.analyzer import ScheduleAnalyzer, ScheduleAlert, ScheduleReport
from schedule_monitoring.data_gatherer import ScheduleDataGatherer, JobSnapshot, CostCodeSnapshot

__all__ = [
    "run_schedule_monitoring",
    "ScheduleAnalyzer",
    "ScheduleAlert",
    "ScheduleReport",
    "ScheduleDataGatherer",
    "JobSnapshot",
    "CostCodeSnapshot",
]
