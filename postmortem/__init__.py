"""
Postmortem Analysis module — Phase 10.

Aggregates completed-project performance data from all existing tracking
modules (budget, quantity, projection, timelog) and produces a per-project
postmortem report.

No new HCSS API calls are required.  All data is read from the existing
database tables that are populated by the daily/monthly cron jobs.

Deferred sections (added automatically once dependencies are complete):
  - Change Order Analysis  → Billing Draft module
  - Schedule Performance   → Schedule Monitoring module

Modules:
    analyzer  — core aggregation engine, per-project PostmortemResult
    schemas   — Pydantic request/response models for the API layer
"""
from __future__ import annotations

from postmortem.analyzer import PostmortemResult, build_postmortem_for_job
from postmortem.schemas import (
    PostmortemListResponse,
    PostmortemDetailResponse,
    PostmortemProjectRow,
)

__all__ = [
    "PostmortemResult",
    "build_postmortem_for_job",
    "PostmortemListResponse",
    "PostmortemDetailResponse",
    "PostmortemProjectRow",
]
