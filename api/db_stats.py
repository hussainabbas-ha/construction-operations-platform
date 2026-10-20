"""
api/db_stats.py
────────────────
CHANGES FROM ORIGINAL:
  + Imported ProjectionTrackingCache, ProjectionTrackingResult   (← NEW)
  + Added projection stats block inside get_comprehensive_db_stats()  (← NEW)
  + Added projection_tracking key to return dict                  (← NEW)
  + Added projection counts to summary totals                     (← NEW)

Search for "← NEW" to find every change.
All existing code is unchanged.
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.orm import Session

from api.database import (
    TimelogVerificationCache,
    TimelogTimecardResult,
    QuantityTrackingCache,
    QuantityTrackingResult,
    BudgetTrackingCache,
    BudgetTrackingResult,
    ProjectionTrackingCache,    # ← NEW
    ProjectionTrackingResult,   # ← NEW
)

logger = logging.getLogger(__name__)


def get_comprehensive_db_stats(db: Session) -> dict[str, Any]:
    """
    Get comprehensive statistics about all tracking modules in the database.

    Returns:
        Dictionary with statistics for all modules:
          - timelog_verification
          - quantity_tracking
          - budget_tracking
          - projection_tracking   ← NEW
          - summary
    """
    try:
        # ── Timelog stats ──────────────────────────────────────────────────
        timelog_cache_count  = db.query(TimelogVerificationCache).count()
        timelog_detail_count = db.query(TimelogTimecardResult).count()
        timelog_oldest = db.query(TimelogVerificationCache).order_by(
            TimelogVerificationCache.verification_date.asc()
        ).first()
        timelog_newest = db.query(TimelogVerificationCache).order_by(
            TimelogVerificationCache.verification_date.desc()
        ).first()

        # ── Quantity stats ─────────────────────────────────────────────────
        quantity_cache_count  = db.query(QuantityTrackingCache).count()
        quantity_detail_count = db.query(QuantityTrackingResult).count()
        quantity_oldest = db.query(QuantityTrackingCache).order_by(
            QuantityTrackingCache.tracking_date.asc()
        ).first()
        quantity_newest = db.query(QuantityTrackingCache).order_by(
            QuantityTrackingCache.tracking_date.desc()
        ).first()

        # ── Budget stats ───────────────────────────────────────────────────
        budget_cache_count  = db.query(BudgetTrackingCache).count()
        budget_detail_count = db.query(BudgetTrackingResult).count()
        budget_oldest = db.query(BudgetTrackingCache).order_by(
            BudgetTrackingCache.tracking_date.asc()
        ).first()
        budget_newest = db.query(BudgetTrackingCache).order_by(
            BudgetTrackingCache.tracking_date.desc()
        ).first()

        # ── Projection stats  ← NEW ────────────────────────────────────────
        projection_cache_count  = db.query(ProjectionTrackingCache).count()
        projection_detail_count = db.query(ProjectionTrackingResult).count()
        projection_oldest = db.query(ProjectionTrackingCache).order_by(
            ProjectionTrackingCache.tracking_month.asc()
        ).first()
        projection_newest = db.query(ProjectionTrackingCache).order_by(
            ProjectionTrackingCache.tracking_month.desc()
        ).first()

        # Status breakdown for projection  ← NEW
        projection_on_track    = (
            db.query(ProjectionTrackingResult)
            .filter(ProjectionTrackingResult.status == "ON_TRACK")
            .count()
        )
        projection_at_risk     = (
            db.query(ProjectionTrackingResult)
            .filter(ProjectionTrackingResult.status == "AT_RISK")
            .count()
        )
        projection_over_budget = (
            db.query(ProjectionTrackingResult)
            .filter(ProjectionTrackingResult.status == "OVER_BUDGET")
            .count()
        )
        projection_alerts      = (
            db.query(ProjectionTrackingResult)
            .filter(ProjectionTrackingResult.alert == True)   # noqa: E712
            .count()
        )

        return {
            # ── unchanged ──────────────────────────────────────────────────
            "timelog_verification": {
                "total_cache_entries":    timelog_cache_count,
                "total_cached_timecards": timelog_detail_count,
                "oldest_cached_date":     timelog_oldest.verification_date if timelog_oldest else None,
                "newest_cached_date":     timelog_newest.verification_date if timelog_newest else None,
            },
            "quantity_tracking": {
                "total_cache_entries":      quantity_cache_count,
                "total_cached_cost_codes":  quantity_detail_count,
                "oldest_cached_date":       quantity_oldest.tracking_date if quantity_oldest else None,
                "newest_cached_date":       quantity_newest.tracking_date if quantity_newest else None,
            },
            "budget_tracking": {
                "total_cache_entries":      budget_cache_count,
                "total_cached_budget_items": budget_detail_count,
                "oldest_cached_date":       budget_oldest.tracking_date if budget_oldest else None,
                "newest_cached_date":       budget_newest.tracking_date if budget_newest else None,
            },

            # ── NEW ────────────────────────────────────────────────────────
            "projection_tracking": {
                "total_cache_entries":       projection_cache_count,
                "total_cached_projections":  projection_detail_count,
                "oldest_cached_month":       projection_oldest.tracking_month if projection_oldest else None,
                "newest_cached_month":       projection_newest.tracking_month if projection_newest else None,
                "status_breakdown": {
                    "on_track":    projection_on_track,
                    "at_risk":     projection_at_risk,
                    "over_budget": projection_over_budget,
                    "alerts":      projection_alerts,
                },
            },

            # ── summary — updated to include projection totals ← NEW ───────
            "summary": {
                "total_cache_entries": (
                    timelog_cache_count
                    + quantity_cache_count
                    + budget_cache_count
                    + projection_cache_count        # ← NEW
                ),
                "total_detail_records": (
                    timelog_detail_count
                    + quantity_detail_count
                    + budget_detail_count
                    + projection_detail_count       # ← NEW
                ),
                "modules_active": sum([
                    1 if timelog_cache_count     > 0 else 0,
                    1 if quantity_cache_count    > 0 else 0,
                    1 if budget_cache_count      > 0 else 0,
                    1 if projection_cache_count  > 0 else 0,  # ← NEW
                ]),
            },
        }

    except Exception as e:
        logger.exception("Failed to get database stats: %s", e)
        return {
            "error": str(e),
            "timelog_verification":  {"total_cache_entries": 0, "total_cached_timecards": 0},
            "quantity_tracking":     {"total_cache_entries": 0, "total_cached_cost_codes": 0},
            "budget_tracking":       {"total_cache_entries": 0, "total_cached_budget_items": 0},
            "projection_tracking":   {"total_cache_entries": 0, "total_cached_projections": 0},  # ← NEW
            "summary": {"total_cache_entries": 0, "total_detail_records": 0, "modules_active": 0},
        }