"""
Unified cron job for all tracking modules (timelog, quantity, budget).
Runs every 3 hours and ensures all data is up-to-date with retry logic.

Sync strategy
-------------
1. Lookback (Layer A): always refresh the last LOOKBACK_DAYS (all modules).
2. modifiedSince (Layer B):
   - Budget: jobCosts/material/subcontract dirty dates (existing).
   - Quantity: timeCardInfo?modifiedSince dirty dates (transaction dates).
   Refresh each module only for dirty dates outside the lookback window.
3. Advance each module's sync cursor only when that module's discovery +
   refreshes all succeed.
"""
from __future__ import annotations

import logging
import os
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy.orm import Session
from sqlalchemy import and_

from api.database import SessionLocal, create_tables, CronJobExecution
from api.db_services import (
    store_timelog_verification,
    store_quantity_tracking,
    store_budget_tracking,
    store_projection_tracking,
    store_postmortem_data,
    create_cron_job_execution,
    update_cron_job_status,
    get_pending_retry_jobs,
    schedule_retry,
    check_if_date_processed,
    get_budget_last_sync_at,
    set_budget_last_sync_at,
    get_quantity_last_sync_at,
    set_quantity_last_sync_at,
    cleanup_quantity_period_cache_keys,
)
from payroll_verification.verifier import verify_payroll_date
from quantity_tracking.tracker import track_quantities, discover_modified_timecard_dates
from payroll_verification.hcss_client import HCSSClient
from budget_tracking.hcss_client import BudgetHCSSClient
from budget_tracking.analyzer import calculate_budget_status

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)-8s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


# ===========================================================================
# CONFIGURATION
# ===========================================================================

# How many days back to check for missing data (all modules)
LOOKBACK_DAYS = 7

# Maximum retries for failed jobs
MAX_RETRIES = 3

# Retry delay in minutes
RETRY_DELAY_MINUTES = 60

# Overlap buffer so we never miss edge SyncTimestamps between runs
BUDGET_MODIFIED_SINCE_OVERLAP_MINUTES = int(
    os.getenv("BUDGET_MODIFIED_SINCE_OVERLAP_MINUTES", "15")
)

# First-run bootstrap window when no budget_last_sync_at cursor exists yet
BUDGET_MODIFIED_SINCE_BOOTSTRAP_HOURS = int(
    os.getenv("BUDGET_MODIFIED_SINCE_BOOTSTRAP_HOURS", "24")
)

# Quantity dirty discovery (timeCardInfo?modifiedSince) — mirrors budget knobs
QUANTITY_MODIFIED_SINCE_OVERLAP_MINUTES = int(
    os.getenv(
        "QUANTITY_MODIFIED_SINCE_OVERLAP_MINUTES",
        str(BUDGET_MODIFIED_SINCE_OVERLAP_MINUTES),
    )
)
QUANTITY_MODIFIED_SINCE_BOOTSTRAP_HOURS = int(
    os.getenv(
        "QUANTITY_MODIFIED_SINCE_BOOTSTRAP_HOURS",
        str(BUDGET_MODIFIED_SINCE_BOOTSTRAP_HOURS),
    )
)

# Ignore dirty transaction dates older than this (YYYY-MM-DD)
BUDGET_SYNC_START_DATE = date.fromisoformat(
    os.getenv("BUDGET_SYNC_START_DATE", "2026-04-01")
)


# ===========================================================================
# INDIVIDUAL MODULE PROCESSORS
# ===========================================================================

def process_timelog_for_date(db: Session, target_date: str) -> tuple[bool, int, str | None]:
    """
    Process time log verification for a specific date.

    Returns:
        Tuple of (success, records_processed, error_message)
    """
    try:
        logger.info("Processing timelog for date: %s", target_date)

        # Fetch and verify timecards
        results = verify_payroll_date(target_date=target_date, business_unit_id=None)

        # Convert results to dict format
        results_dict = [
            {
                "id": r.id,
                "date": r.date,
                "job": r.job_code,  # Fixed: was r.job, should be r.job_code
                "job_id": r.job_id,
                "job_description": r.job_description,
                "foreman": r.foreman,
                "foreman_id": r.foreman_id,
                "status": r.status,
                "reasons": r.reasons,
                "why": r.why,
            }
            for r in results
        ]

        # Store in database
        cache_id, records_stored = store_timelog_verification(
            db=db,
            verification_date=target_date,
            business_unit_id=None,
            results=results_dict,
        )

        logger.info("✓ Timelog processed: date=%s records=%d", target_date, records_stored)
        return True, records_stored, None

    except Exception as exc:
        error_msg = f"Timelog processing failed: {exc}"
        logger.error(error_msg)
        return False, 0, error_msg


def process_quantity_for_date(db: Session, target_date: str) -> tuple[bool, int, str | None]:
    """
    Process quantity tracking for a specific date.

    Returns:
        Tuple of (success, records_processed, error_message)
    """
    try:
        logger.info("Processing quantity tracking for date: %s", target_date)

        # Track quantities
        results = track_quantities(
            start_date=target_date,
            end_date=target_date,
        )

        # Convert results to dict format
        results_dict = [
            {
                "cost_code_id": r.cost_code_id,
                "cost_code": r.cost_code,
                "description": r.description,
                "job_id": r.job_id,
                "job_code": r.job_code,
                "unit": r.unit,
                "cost_type": r.cost_type,
                "planned_quantity": r.planned_quantity,
                "installed_quantity": r.installed_quantity,
                "remaining_quantity": r.remaining_quantity,
                "percent_complete": r.percent_complete,
                "status": r.status,
                "alert": r.alert,
            }
            for r in results
        ]

        # Store in database
        cache_id, records_stored = store_quantity_tracking(
            db=db,
            tracking_date=target_date,
            results=results_dict,
        )

        logger.info("✓ Quantity tracking processed: date=%s records=%d", target_date, records_stored)
        return True, records_stored, None

    except Exception as exc:
        error_msg = f"Quantity tracking failed: {exc}"
        logger.error(error_msg)
        return False, 0, error_msg


def process_budget_for_date(db: Session, target_date: str) -> tuple[bool, int, str | None]:
    """
    Process budget tracking for a specific date.

    Stores budgeted_all_cost (raw HCSS total) in DB.
    expected_budget is NOT stored — it is computed on the fly at response time
    using: (quantity / planned_quantity) * budgeted_all_cost

    Returns:
        Tuple of (success, records_processed, error_message)
    """
    try:
        logger.info("Processing budget tracking for date: %s", target_date)

        # Fetch budget data from HCSS
        client = BudgetHCSSClient()
        raw_data = client.fetch_budget_summary(date=target_date)

        # Parse into BudgetResult objects (no metrics — just budgeted_all_cost + actual_cost)
        results = calculate_budget_status(raw_data)

        # Build lookup of extra fields from raw HCSS response by (jobId, costCodeId)
        raw_by_key: dict[tuple, dict] = {}
        for item in raw_data:
            jid  = (item.get("job")      or {}).get("jobId",      "")
            ccid = (item.get("costCode") or {}).get("costCodeId", "")
            if jid and ccid:
                raw_by_key[(jid, ccid)] = item

        results_dict = []
        for r in results:
            extra = raw_by_key.get((r.job_id, r.cost_code_id), {})
            results_dict.append({
                "cost_code_id":          r.cost_code_id,
                "cost_code":             r.cost_code,
                "cost_code_description": r.cost_code_description,
                "job_id":                r.job_id,
                "job_name":              r.job_name,
                "business_unit":         r.business_unit,
                # Raw HCSS total budget — stored in DB as budgeted_all_cost
                "budgeted_all_cost":     r.budgeted_all_cost,
                "actual_cost":           r.actual_cost,
                # Actual cost breakdown from raw HCSS response
                "labor_cost":       extra.get("laborCost",       0.0),
                "equipment_cost":   extra.get("equipmentCost",   0.0),
                "material_cost":    extra.get("materialCost",    0.0),
                "subcontract_cost": extra.get("subcontractCost", 0.0),
                "trucking_cost":    extra.get("truckingCost",    0.0),
                "labor_hours":      extra.get("laborHours",      0.0),
                "quantity":         extra.get("quantity",        0.0),
                # Planned budget breakdown from raw HCSS response
                "labor_budget":       extra.get("laborBudget",       0.0),
                "equipment_budget":   extra.get("equipmentBudget",   0.0),
                "material_budget":    extra.get("materialBudget",    0.0),
                "subcontract_budget": extra.get("subcontractBudget", 0.0),
                # Planned quantity + unit from v1/costCodes (info-only, not used in calculations)
                "planned_quantity":   float(extra.get("plannedQuantity", 0.0)),
                "unit":               extra.get("unit", "") or "",
            })

        # Store in database
        cache_id, records_stored = store_budget_tracking(
            db=db,
            tracking_date=target_date,
            results=results_dict,
        )

        logger.info("✓ Budget tracking processed: date=%s records=%d", target_date, records_stored)
        return True, records_stored, None

    except Exception as exc:
        error_msg = f"Budget tracking failed: {exc}"
        logger.error(error_msg)
        return False, 0, error_msg


def process_postmortem(db: Session) -> tuple[bool, int, str | None]:
    """
    Fetch Expected All Cost data for ALL active HCSS jobs and upsert into
    the postmortem_job_snapshots + postmortem_cost_code_results tables.
    Also purges any snapshot rows for jobs no longer active in HCSS.

    This runs once per day (not per date — it's a full snapshot of current
    HCSS state).  The cron key used is today's ISO date so check_if_date_processed
    prevents double-runs within the same calendar day.

    Returns:
        Tuple of (success, jobs_stored, error_message)
    """
    try:
        from postmortem.data_fetcher import fetch_all_jobs_postmortem
        from api.db_services import purge_stale_postmortem_jobs

        logger.info("Processing postmortem data for all jobs ...")
        job_results = fetch_all_jobs_postmortem()

        if not job_results:
            logger.warning("Postmortem: HCSS returned 0 processable jobs")
            return True, 0, None

        jobs_stored = 0
        active_job_ids: list[str] = []

        for job in job_results:
            try:
                store_postmortem_data(
                    db            = db,
                    job_id        = job["job_id"],
                    job_code      = job["job_code"],
                    job_name      = job["job_name"],
                    business_unit = job.get("business_unit"),
                    totals        = job["totals"],
                    cost_codes    = job["cost_codes"],
                )
                active_job_ids.append(job["job_id"])
                jobs_stored += 1
            except Exception as store_exc:
                logger.error(
                    "Postmortem: failed to store job %s (%s): %s",
                    job.get("job_code"), job.get("job_id"), store_exc,
                )
                # Continue — a single bad job must not abort the batch

        # Purge any snapshot rows for jobs that are no longer active in HCSS
        if active_job_ids:
            purged = purge_stale_postmortem_jobs(db, active_job_ids)
            if purged:
                logger.info("Postmortem: purged %d stale job snapshot(s)", purged)

        logger.info("✓ Postmortem processed: %d/%d jobs stored", jobs_stored, len(job_results))
        return True, jobs_stored, None

    except Exception as exc:
        error_msg = f"Postmortem processing failed: {exc}"
        logger.error(error_msg, exc_info=True)
        return False, 0, error_msg


# ===========================================================================
# UNIFIED PROCESSOR
# ===========================================================================

def process_single_module(
    db: Session,
    job_type: str,
    target_date: str,
) -> bool:
    """
    Process a single module for a specific date with retry logic.

    Returns:
        True if successful, False otherwise
    """
    # Check if we have cached data for this date
    if job_type == "timelog":
        from api.database import TimelogVerificationCache
        cached = db.query(TimelogVerificationCache).filter(
            and_(
                TimelogVerificationCache.verification_date == target_date,
                TimelogVerificationCache.business_unit_id == None,
            )
        ).first()
        cached_count = cached.total_timecards if cached else None

    elif job_type == "quantity":
        from api.database import QuantityTrackingCache
        cached = db.query(QuantityTrackingCache).filter(
            QuantityTrackingCache.tracking_date == target_date
        ).first()
        cached_count = cached.total_cost_codes if cached else None

    elif job_type == "budget":
        from api.database import BudgetTrackingCache
        cached = db.query(BudgetTrackingCache).filter(
            BudgetTrackingCache.tracking_date == target_date
        ).first()
        cached_count = cached.total_cost_codes if cached else None
    else:
        cached_count = None

    # If we have cached data, log refetch intent (no smart-skip for any module)
    if cached_count is not None:
        try:
            from payroll_verification.hcss_client import HCSSClient
            client = HCSSClient()

            # Lightweight call kept for parity with prior behavior / rate-limit pacing
            client.fetch_timecards(
                start_date=target_date,
                end_date=target_date,
                business_unit_id=None,
            )

            if job_type == "timelog":
                logger.info(
                    "timelog for %s: always refetching (no skip logic)",
                    target_date,
                )
            elif job_type == "quantity":
                logger.info(
                    "quantity for %s: always refetching (no smart skip)",
                    target_date,
                )
            elif job_type == "budget":
                logger.info(
                    "budget for %s: always refetching (lookback / modifiedSince)",
                    target_date,
                )

        except Exception as e:
            # If the check itself fails, always refetch to be safe
            logger.warning(
                "Could not check if %s data changed for %s: %s — will refetch",
                job_type, target_date, str(e),
            )
    else:
        logger.info("No cached data for %s on %s — fetching fresh", job_type, target_date)

    # Create job execution record
    job_id = create_cron_job_execution(
        db=db,
        job_type=job_type,
        execution_date=target_date,
        max_retries=MAX_RETRIES,
    )

    # Update status to running
    update_cron_job_status(db, job_id, "running")

    # Process based on job type
    if job_type == "timelog":
        success, records, error = process_timelog_for_date(db, target_date)
    elif job_type == "quantity":
        success, records, error = process_quantity_for_date(db, target_date)
    elif job_type == "budget":
        success, records, error = process_budget_for_date(db, target_date)
    else:
        logger.error("Unknown job type: %s", job_type)
        return False

    # Update job status
    if success:
        update_cron_job_status(
            db=db,
            job_id=job_id,
            status="success",
            records_processed=records,
        )
        return True
    else:
        update_cron_job_status(
            db=db,
            job_id=job_id,
            status="failed",
            error_message=error,
        )

        # Schedule retry if not max retries reached
        schedule_retry(db, job_id, RETRY_DELAY_MINUTES)
        logger.warning("Scheduled retry for %s on %s", job_type, target_date)
        return False


def process_all_modules_for_date(db: Session, target_date: str) -> dict[str, bool]:
    """
    Process all three modules for a specific date.

    Returns:
        Dict of {module_name: success_status}
    """
    results = {}

    for job_type in ["timelog", "quantity", "budget"]:
        try:
            success = process_single_module(db, job_type, target_date)
            results[job_type] = success

            # Add delay between modules to avoid rate limiting
            if job_type != "budget":  # Don't delay after the last module
                time.sleep(10)  # 10 second delay between modules

        except Exception as exc:
            logger.error("Unexpected error processing %s for %s: %s", job_type, target_date, exc)
            results[job_type] = False

    return results


# ===========================================================================
# BUDGET modifiedSince DIRTY-DATE DISCOVERY
# ===========================================================================

def _budget_modified_since_cutoff(db: Session, sync_started_at: datetime) -> datetime:
    """
    Compute modifiedSince for HCSS discovery:
      last_sync - overlap   (normal)
      now - bootstrap hours (first run, no cursor yet)
    """
    last = get_budget_last_sync_at(db)
    if last is None:
        cutoff = sync_started_at - timedelta(hours=BUDGET_MODIFIED_SINCE_BOOTSTRAP_HOURS)
        logger.info(
            "No budget_last_sync_at cursor — bootstrapping modifiedSince to %s (%dh)",
            cutoff.isoformat(),
            BUDGET_MODIFIED_SINCE_BOOTSTRAP_HOURS,
        )
        return cutoff

    cutoff = last - timedelta(minutes=BUDGET_MODIFIED_SINCE_OVERLAP_MINUTES)
    logger.info(
        "budget modifiedSince cutoff=%s (last_sync=%s, overlap=%dm)",
        cutoff.isoformat(),
        last.isoformat(),
        BUDGET_MODIFIED_SINCE_OVERLAP_MINUTES,
    )
    return cutoff


def discover_budget_dirty_dates(
    db: Session,
    sync_started_at: datetime,
) -> tuple[set[str], bool]:
    """
    Ask HCSS which transaction dates had cost changes since last sync.

    Returns:
        (dirty_dates as YYYY-MM-DD set, discovery_ok)
        discovery_ok=False means the cursor must NOT be advanced.
    """
    yesterday = date.today() - timedelta(days=1)
    min_date = BUDGET_SYNC_START_DATE
    max_date = yesterday

    if min_date > max_date:
        logger.warning(
            "BUDGET_SYNC_START_DATE %s is after yesterday %s — skipping dirty discovery",
            min_date, max_date,
        )
        return set(), True

    cutoff = _budget_modified_since_cutoff(db, sync_started_at)

    try:
        client = BudgetHCSSClient()
        dirty = client.discover_modified_transaction_dates(
            cutoff,
            min_date=min_date,
            max_date=max_date,
        )
        logger.info(
            "Budget dirty-date discovery OK: %d date(s)%s",
            len(dirty),
            f" → {sorted(dirty)}" if dirty else "",
        )
        return dirty, True
    except Exception as exc:
        logger.error("Budget dirty-date discovery FAILED: %s", exc, exc_info=True)
        return set(), False


# ===========================================================================
# QUANTITY modifiedSince DIRTY-DATE DISCOVERY (timeCardInfo)
# ===========================================================================

def _quantity_modified_since_cutoff(db: Session, sync_started_at: datetime) -> datetime:
    """
    Compute modifiedSince for quantity dirty discovery:
      last_sync - overlap   (normal)
      now - bootstrap hours (first run, no cursor yet)
    """
    last = get_quantity_last_sync_at(db)
    if last is None:
        cutoff = sync_started_at - timedelta(hours=QUANTITY_MODIFIED_SINCE_BOOTSTRAP_HOURS)
        logger.info(
            "No quantity_last_sync_at cursor — bootstrapping modifiedSince to %s (%dh)",
            cutoff.isoformat(),
            QUANTITY_MODIFIED_SINCE_BOOTSTRAP_HOURS,
        )
        return cutoff

    cutoff = last - timedelta(minutes=QUANTITY_MODIFIED_SINCE_OVERLAP_MINUTES)
    logger.info(
        "quantity modifiedSince cutoff=%s (last_sync=%s, overlap=%dm)",
        cutoff.isoformat(),
        last.isoformat(),
        QUANTITY_MODIFIED_SINCE_OVERLAP_MINUTES,
    )
    return cutoff


def discover_quantity_dirty_dates(
    db: Session,
    sync_started_at: datetime,
) -> tuple[set[str], bool]:
    """
    Ask HCSS which timecard transaction dates were modified since last sync.

    Uses GET /v1/timeCardInfo?modifiedSince=... (proven quantity dirty channel).

    Returns:
        (dirty_dates as YYYY-MM-DD set, discovery_ok)
        discovery_ok=False means the quantity cursor must NOT be advanced.
    """
    yesterday = date.today() - timedelta(days=1)
    min_date = BUDGET_SYNC_START_DATE
    max_date = yesterday

    if min_date > max_date:
        logger.warning(
            "BUDGET_SYNC_START_DATE %s is after yesterday %s — skipping qty dirty discovery",
            min_date, max_date,
        )
        return set(), True

    cutoff = _quantity_modified_since_cutoff(db, sync_started_at)

    try:
        client = HCSSClient()
        dirty = discover_modified_timecard_dates(
            client,
            cutoff,
            min_date=min_date,
            max_date=max_date,
        )
        logger.info(
            "Quantity dirty-date discovery OK: %d date(s)%s",
            len(dirty),
            f" → {sorted(dirty)}" if dirty else "",
        )
        return dirty, True
    except Exception as exc:
        logger.error("Quantity dirty-date discovery FAILED: %s", exc, exc_info=True)
        return set(), False


# ===========================================================================
# MAIN CRON JOB LOGIC
# ===========================================================================

def process_pending_retries(db: Session) -> None:
    """
    Process all jobs that are scheduled for retry.
    """
    retry_jobs = get_pending_retry_jobs(db)

    if not retry_jobs:
        logger.info("No pending retries")
        return

    logger.info("Found %d jobs pending retry", len(retry_jobs))

    for job in retry_jobs:
        logger.info(
            "Retrying %s for %s (attempt %d/%d)",
            job.job_type,
            job.execution_date,
            job.attempt_count,
            job.max_retries,
        )

        # Update status to running
        update_cron_job_status(db, job.id, "running")

        # Process based on job type
        if job.job_type == "timelog":
            success, records, error = process_timelog_for_date(db, job.execution_date)
        elif job.job_type == "quantity":
            success, records, error = process_quantity_for_date(db, job.execution_date)
        elif job.job_type == "budget":
            success, records, error = process_budget_for_date(db, job.execution_date)
        elif job.job_type == "postmortem":
            success, records, error = process_postmortem(db)
        else:
            continue

        # Update job status
        if success:
            update_cron_job_status(
                db=db,
                job_id=job.id,
                status="success",
                records_processed=records,
            )
            logger.info("✓ Retry successful for %s on %s", job.job_type, job.execution_date)
        else:
            update_cron_job_status(
                db=db,
                job_id=job.id,
                status="failed",
                error_message=error,
            )

            # Schedule another retry if not max retries reached
            if job.attempt_count < job.max_retries:
                schedule_retry(db, job.id, RETRY_DELAY_MINUTES)
                logger.warning("Scheduled another retry for %s on %s", job.job_type, job.execution_date)
            else:
                logger.error("Max retries reached for %s on %s", job.job_type, job.execution_date)

        # Delay between retried jobs to avoid rate limiting
        time.sleep(5)


def get_dates_to_process() -> list[str]:
    """
    Get lookback dates for all modules (yesterday going back LOOKBACK_DAYS).
    """
    today = date.today()
    yesterday = today - timedelta(days=1)

    dates = []
    for i in range(LOOKBACK_DAYS):
        target_date = yesterday - timedelta(days=i)
        dates.append(target_date.isoformat())

    return dates


def run_unified_cron_job() -> None:
    """
    Main cron job entry point.

    1. Pending retries
    2. One-time cleanup of legacy quantity period (start__end) cache keys
    3. Lookback dates → timelog + quantity + budget
    4. modifiedSince dirty dates outside lookback → quantity-only / budget-only
    5. Advance each module cursor only if that module's discovery + refreshes OK
    """
    logger.info("=" * 80)
    logger.info("UNIFIED CRON JOB STARTED")
    logger.info("=" * 80)

    # Ensure tables exist (includes sync_state)
    create_tables()

    # Create database session
    db = SessionLocal()

    try:
        # Step 0: Drop legacy period snapshot keys (idempotent)
        try:
            cleaned = cleanup_quantity_period_cache_keys(db)
            if cleaned:
                logger.info("Step 0: Removed %d legacy quantity period-key row(s)", cleaned)
        except Exception as exc:
            logger.error("Step 0: quantity period-key cleanup failed: %s", exc)

        # Step 1: Process pending retries first
        logger.info("Step 1: Processing pending retries...")
        process_pending_retries(db)

        # Capture sync start BEFORE discovery so we don't miss edits that land
        # while this run is in progress.
        sync_started_at = datetime.now(timezone.utc)

        # Step 2a: Lookback dates (all modules)
        lookback_dates = get_dates_to_process()
        lookback_set = set(lookback_dates)
        logger.info(
            "Step 2a: Lookback — %d date(s) for timelog/quantity/budget: %s",
            len(lookback_dates),
            lookback_dates,
        )

        # Step 2b: Discover historically-dated budget edits via modifiedSince
        logger.info("Step 2b: Budget modifiedSince dirty-date discovery...")
        budget_dirty_dates, budget_discovery_ok = discover_budget_dirty_dates(
            db, sync_started_at
        )
        budget_extra_dates = sorted(
            d for d in budget_dirty_dates if d not in lookback_set
        )
        logger.info(
            "Step 2b: %d budget dirty date(s) total, %d outside lookback%s",
            len(budget_dirty_dates),
            len(budget_extra_dates),
            f": {budget_extra_dates}" if budget_extra_dates else "",
        )

        # Step 2c: Discover quantity dirty dates via timeCardInfo?modifiedSince
        logger.info("Step 2c: Quantity timeCardInfo dirty-date discovery...")
        qty_dirty_dates, qty_discovery_ok = discover_quantity_dirty_dates(
            db, sync_started_at
        )
        qty_extra_dates = sorted(
            d for d in qty_dirty_dates if d not in lookback_set
        )
        logger.info(
            "Step 2c: %d quantity dirty date(s) total, %d outside lookback%s",
            len(qty_dirty_dates),
            len(qty_extra_dates),
            f": {qty_extra_dates}" if qty_extra_dates else "",
        )

        # Step 3: Process lookback dates (all modules)
        total_processed = 0
        total_failed = 0
        budget_refresh_ok = True
        quantity_refresh_ok = True

        for i, target_date in enumerate(lookback_dates):
            logger.info("-" * 80)
            logger.info("Processing date: %s", target_date)

            results = process_all_modules_for_date(db, target_date)

            successes = sum(1 for success in results.values() if success)
            failures = sum(1 for success in results.values() if not success)

            total_processed += successes
            total_failed += failures

            if not results.get("budget", False):
                budget_refresh_ok = False
            if not results.get("quantity", False):
                quantity_refresh_ok = False

            logger.info(
                "Date %s: %d/%d modules successful",
                target_date,
                successes,
                len(results),
            )

            # Delay between dates (and before dirty extras)
            if i < len(lookback_dates) - 1 or qty_extra_dates or budget_extra_dates:
                time.sleep(15)

        # Step 4a: Quantity-only refresh for dirty dates outside lookback
        if qty_extra_dates:
            logger.info("-" * 80)
            logger.info(
                "Step 4a: Quantity-only refresh for %d dirty date(s)...",
                len(qty_extra_dates),
            )
            for j, target_date in enumerate(qty_extra_dates):
                logger.info("-" * 80)
                logger.info("Quantity dirty refresh: %s", target_date)
                try:
                    ok = process_single_module(db, "quantity", target_date)
                except Exception as exc:
                    logger.error(
                        "Unexpected error on quantity dirty refresh %s: %s",
                        target_date, exc,
                    )
                    ok = False

                if ok:
                    total_processed += 1
                else:
                    total_failed += 1
                    quantity_refresh_ok = False

                if j < len(qty_extra_dates) - 1 or budget_extra_dates:
                    time.sleep(15)

        # Step 4b: Budget-only refresh for dirty dates outside lookback
        if budget_extra_dates:
            logger.info("-" * 80)
            logger.info(
                "Step 4b: Budget-only refresh for %d modifiedSince date(s)...",
                len(budget_extra_dates),
            )
            for j, target_date in enumerate(budget_extra_dates):
                logger.info("-" * 80)
                logger.info("Budget dirty refresh: %s", target_date)
                try:
                    ok = process_single_module(db, "budget", target_date)
                except Exception as exc:
                    logger.error(
                        "Unexpected error on budget dirty refresh %s: %s",
                        target_date, exc,
                    )
                    ok = False

                if ok:
                    total_processed += 1
                else:
                    total_failed += 1
                    budget_refresh_ok = False

                if j < len(budget_extra_dates) - 1:
                    time.sleep(15)

        # Step 5: Advance sync cursors only on full module success
        if qty_discovery_ok and quantity_refresh_ok:
            set_quantity_last_sync_at(db, sync_started_at)
            logger.info(
                "✓ Advanced quantity_last_sync_at → %s",
                sync_started_at.isoformat(),
            )
        else:
            logger.warning(
                "NOT advancing quantity_last_sync_at "
                "(discovery_ok=%s quantity_refresh_ok=%s) — next run will rediscover",
                qty_discovery_ok,
                quantity_refresh_ok,
            )

        if budget_discovery_ok and budget_refresh_ok:
            set_budget_last_sync_at(db, sync_started_at)
            logger.info(
                "✓ Advanced budget_last_sync_at → %s",
                sync_started_at.isoformat(),
            )
        else:
            logger.warning(
                "NOT advancing budget_last_sync_at "
                "(discovery_ok=%s budget_refresh_ok=%s) — next run will rediscover",
                budget_discovery_ok,
                budget_refresh_ok,
            )

        # Summary
        logger.info("=" * 80)
        logger.info("CRON JOB COMPLETED")
        logger.info("Total successful: %d", total_processed)
        logger.info("Total failed: %d", total_failed)
        logger.info(
            "Quantity dirty extras: %d | discovery_ok=%s | cursor_advanced=%s",
            len(qty_extra_dates),
            qty_discovery_ok,
            qty_discovery_ok and quantity_refresh_ok,
        )
        logger.info(
            "Budget dirty extras: %d | discovery_ok=%s | cursor_advanced=%s",
            len(budget_extra_dates),
            budget_discovery_ok,
            budget_discovery_ok and budget_refresh_ok,
        )
        logger.info("=" * 80)

    except Exception as exc:
        logger.exception("Fatal error in cron job: %s", exc)
    finally:
        db.close()


# ===========================================================================
# ENTRY POINT
# ===========================================================================

if __name__ == "__main__":
    run_unified_cron_job()
