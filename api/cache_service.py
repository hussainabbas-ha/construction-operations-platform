"""
Cache service for time log verification results.
Handles storing and retrieving verification data from the database.
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from api.database import TimelogTimecardResult, TimelogVerificationCache
from payroll_verification.verifier import TimecardResult

logger = logging.getLogger(__name__)


def _cache_key(date: str, business_unit_id: str | None) -> tuple[str, str | None]:
    """Normalize cache key components."""
    return (date, business_unit_id or None)


def get_cached_verification(
    db: Session,
    verification_date: str,
    business_unit_id: str | None = None,
) -> tuple[dict[str, int], list[TimecardResult]] | None:
    """
    Retrieve cached verification results for a specific date and business unit.
    
    Returns:
        Tuple of (summary_dict, timecard_results) if cache exists, None otherwise.
        summary_dict contains: {total, approved, flagged, rejected}
    """
    date_key, bu_key = _cache_key(verification_date, business_unit_id)
    
    logger.debug(
        "Checking cache for date=%s bu=%s",
        date_key, bu_key or "all"
    )
    
    # Check if cache entry exists
    cache_entry = db.query(TimelogVerificationCache).filter(
        TimelogVerificationCache.verification_date == date_key,
        TimelogVerificationCache.business_unit_id == bu_key,
    ).first()
    
    if not cache_entry:
        logger.debug("Cache miss for date=%s bu=%s", date_key, bu_key or "all")
        return None
    
    logger.info(
        "Cache hit for date=%s bu=%s (fetched_at=%s)",
        date_key, bu_key or "all", cache_entry.fetched_at.isoformat()
    )
    
    # Retrieve timecard results
    timecard_rows = db.query(TimelogTimecardResult).filter(
        TimelogTimecardResult.verification_date == date_key,
        TimelogTimecardResult.business_unit_id == bu_key,
    ).all()
    
    # Convert to TimecardResult objects
    results: list[TimecardResult] = []
    for row in timecard_rows:
        results.append(TimecardResult(
            id=row.timecard_id,
            date=row.verification_date,
            business_unit_id=row.business_unit_id,
            job_id=row.job_id,
            job_code=row.job_code,
            foreman_id=row.foreman_id,
            foreman=row.foreman,
            status=row.status,  # type: ignore[arg-type]
            reasons=json.loads(row.reasons) if row.reasons else [],
            job_description=getattr(row, "job_description", "") or "",
            portal_sync_status=getattr(row, "portal_sync_status", "pending") or "pending",
            portal_synced_at=(row.portal_synced_at.isoformat()
                              if getattr(row, "portal_synced_at", None) else None),
        ))
    
    summary = {
        "total": cache_entry.total_timecards,
        "approved": cache_entry.approved_count,
        "flagged": cache_entry.flagged_count,
        "rejected": cache_entry.rejected_count,
    }
    
    logger.debug(
        "Retrieved %d cached timecard results for date=%s",
        len(results), date_key
    )
    
    return summary, results


def store_verification_results(
    db: Session,
    verification_date: str,
    business_unit_id: str | None,
    results: list[TimecardResult],
) -> None:
    """
    Store verification results in the cache.
    
    This will:
    1. Delete any existing cache entry for this date/business_unit
    2. Create a new cache entry with summary counts
    3. Store all individual timecard results
    """
    date_key, bu_key = _cache_key(verification_date, business_unit_id)
    
    logger.info(
        "Storing %d verification results in cache for date=%s bu=%s",
        len(results), date_key, bu_key or "all"
    )
    
    # Calculate summary counts
    approved = sum(1 for r in results if r.status == "APPROVED")
    flagged = sum(1 for r in results if r.status == "FLAGGED")
    rejected = sum(1 for r in results if r.status == "REJECTED")
    
    try:
        # Delete existing cache entry and related timecards (if any)
        db.query(TimelogTimecardResult).filter(
            TimelogTimecardResult.verification_date == date_key,
            TimelogTimecardResult.business_unit_id == bu_key,
        ).delete()
        
        db.query(TimelogVerificationCache).filter(
            TimelogVerificationCache.verification_date == date_key,
            TimelogVerificationCache.business_unit_id == bu_key,
        ).delete()
        
        # Create new cache entry
        cache_entry = TimelogVerificationCache(
            id=str(uuid.uuid4()),
            verification_date=date_key,
            business_unit_id=bu_key,
            total_timecards=len(results),
            approved_count=approved,
            flagged_count=flagged,
            rejected_count=rejected,
            fetched_at=datetime.now(timezone.utc),
        )
        db.add(cache_entry)
        
        # Store individual timecard results
        for result in results:
            timecard_row = TimelogTimecardResult(
                id=str(uuid.uuid4()),
                verification_date=date_key,
                business_unit_id=bu_key,
                timecard_id=result.id,
                job_id=result.job_id,
                job_code=result.job_code,
                job_description=getattr(result, "job_description", "") or "",
                foreman_id=result.foreman_id,
                foreman=result.foreman,
                status=result.status,
                reasons=json.dumps(result.reasons),
                why=result.why,
            )
            db.add(timecard_row)
        
        db.commit()
        
        logger.info(
            "Successfully cached %d timecard results for date=%s (approved=%d, flagged=%d, rejected=%d)",
            len(results), date_key, approved, flagged, rejected
        )
        
    except Exception as e:
        db.rollback()
        logger.exception(
            "Failed to store verification results in cache for date=%s: %s",
            date_key, e
        )
        raise


def clear_cache_for_date(
    db: Session,
    verification_date: str,
    business_unit_id: str | None = None,
) -> int:
    """
    Clear cached verification results for a specific date.
    
    Returns:
        Number of cache entries deleted.
    """
    date_key, bu_key = _cache_key(verification_date, business_unit_id)
    
    logger.info("Clearing cache for date=%s bu=%s", date_key, bu_key or "all")
    
    try:
        # Delete timecard results
        timecard_count = db.query(TimelogTimecardResult).filter(
            TimelogTimecardResult.verification_date == date_key,
            TimelogTimecardResult.business_unit_id == bu_key,
        ).delete()
        
        # Delete cache entry
        cache_count = db.query(TimelogVerificationCache).filter(
            TimelogVerificationCache.verification_date == date_key,
            TimelogVerificationCache.business_unit_id == bu_key,
        ).delete()
        
        db.commit()
        
        logger.info(
            "Cleared cache for date=%s: %d cache entries, %d timecard results",
            date_key, cache_count, timecard_count
        )
        
        return cache_count
        
    except Exception as e:
        db.rollback()
        logger.exception("Failed to clear cache for date=%s: %s", date_key, e)
        raise


def get_cache_stats(db: Session) -> dict[str, Any]:
    """
    Get statistics about the cache.
    
    Returns:
        Dictionary with cache statistics.
    """
    try:
        total_cache_entries = db.query(TimelogVerificationCache).count()
        total_timecards = db.query(TimelogTimecardResult).count()
        
        # Get date range
        oldest = db.query(TimelogVerificationCache).order_by(
            TimelogVerificationCache.verification_date.asc()
        ).first()
        newest = db.query(TimelogVerificationCache).order_by(
            TimelogVerificationCache.verification_date.desc()
        ).first()
        
        return {
            "total_cache_entries": total_cache_entries,
            "total_cached_timecards": total_timecards,
            "oldest_cached_date": oldest.verification_date if oldest else None,
            "newest_cached_date": newest.verification_date if newest else None,
        }
    except Exception as e:
        logger.exception("Failed to get cache stats: %s", e)
        return {
            "error": str(e),
            "total_cache_entries": 0,
            "total_cached_timecards": 0,
        }
