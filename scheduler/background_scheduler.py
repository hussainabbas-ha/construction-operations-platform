from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from scheduler.unified_cron_job import run_unified_cron_job, process_postmortem
from scheduler.monthly_projection_job import run_monthly_projection_job   # ← NEW

from scheduler.daily_job import run_daily_time_log_report
from scheduler.weekly_quantity_job import run_weekly_quantity_alerts

from scheduler.budget_alert_job import run_daily_budget_alert, run_weekly_budget_summary

logger = logging.getLogger(__name__)

_MT = ZoneInfo("America/Denver")

scheduler: BackgroundScheduler | None = None

# Holds the Postgres connection that owns the scheduler advisory lock, kept open
# for the process lifetime so the lock persists (only one worker schedules jobs).
_scheduler_lock_conn = None
# Arbitrary unique bigint key for the scheduler advisory lock.
_SCHEDULER_LOCK_KEY = 911_000_001


def _acquire_scheduler_lock() -> bool:
    """Return True only for the single process that should own the scheduler.

    With multiple uvicorn/gunicorn workers, each process otherwise starts its
    own BackgroundScheduler, so every cron job (daily/weekly/monthly emails,
    portal sync) fires once PER worker — that's the "email sent 4 times" bug.

    A Postgres session-level advisory lock fixes this: only the worker that
    grabs the lock starts the scheduler; the lock is held for the process
    lifetime via a dedicated open connection. On non-Postgres (local dev) there
    is a single process, so we just return True.
    """
    global _scheduler_lock_conn
    try:
        from sqlalchemy import text
        from api.database import engine

        if engine.dialect.name != "postgresql":
            return True  # dev / single-process — no guard needed

        conn = engine.connect()
        acquired = conn.execute(
            text("SELECT pg_try_advisory_lock(:k)"), {"k": _SCHEDULER_LOCK_KEY}
        ).scalar()
        if acquired:
            _scheduler_lock_conn = conn  # keep open -> lock held for process life
            logger.info("Scheduler advisory lock acquired — this worker owns the scheduler")
            return True
        conn.close()
        logger.info("Scheduler advisory lock held by another worker — not scheduling here")
        return False
    except Exception:
        # Fail open so a transient DB hiccup never leaves the system with NO
        # scheduler at all. Worst case is the pre-existing duplicate behaviour.
        logger.exception("Scheduler advisory lock check failed — starting scheduler anyway")
        return True


# ─────────────────────────────────────────────
# EMAIL TOGGLE (NEW)
# ─────────────────────────────────────────────
EMAILS_ENABLED = os.getenv("EMAILS_ENABLED", "true").lower() == "true"


def run_unified_sync_job() -> None:
    """
    Entry point called by APScheduler every 8 hours.
    Handles: timelog, quantity, budget (daily modules).
    Projection is handled separately by the monthly job below.
    """
    run_unified_cron_job()


def _run_daily_report() -> None:
    """Read DB cache, build report, email the team. Runs at 1 PM MT daily."""
    run_daily_time_log_report()


def _run_daily_portal_sync() -> None:
    """Browser-automate Approve/Reject on HeavyJob and write back each card's
    sync status. Runs at 12:30 PM MT — before the 1 PM email — so the slow
    automation never delays the report.

    Playwright is async; we always drive it with a fresh event loop in a
    dedicated thread so gunicorn/uvicorn's running loop never conflicts.
    """
    import asyncio
    from concurrent.futures import ThreadPoolExecutor

    from scheduler.daily_job import run_daily_portal_sync

    def _run() -> None:
        asyncio.run(run_daily_portal_sync())

    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="portal-sync") as pool:
        pool.submit(_run).result()


def _run_weekly_quantity() -> None:
    """Fetch quantity data, check 75% threshold, email alerts. Runs Friday 8 AM MT."""
    run_weekly_quantity_alerts()


def _run_daily_budget_alert() -> None:
    run_daily_budget_alert()


def _run_weekly_budget_summary() -> None:
    run_weekly_budget_summary()


def _run_monthly_billing_draft() -> None:
    """Build + store the previous month's billing draft. Runs on the 1st, MT."""
    from billing_draft_tracking.builder import run_monthly_billing_draft
    run_monthly_billing_draft()
def _run_weekly_postmortem() -> None:
    """
    Run postmortem data fetch for all active jobs.
    Fires Saturday at 6:00 AM Mountain Time.

    Fetches Expected All Cost + actual cost per cost code for every active HCSS
    job, stores results in postmortem_job_snapshots + postmortem_cost_code_results,
    and purges any stale (inactive/completed) job rows.
    """
    from api.database import SessionLocal, create_tables

    logger.info("Weekly postmortem job starting ...")
    create_tables()
    db = SessionLocal()
    try:
        success, jobs_stored, error = process_postmortem(db)
        if success:
            logger.info("✓ Weekly postmortem complete: %d job(s) stored", jobs_stored)
        else:
            logger.error("Weekly postmortem failed: %s", error)
    except Exception as exc:
        logger.exception("Weekly postmortem job raised an unexpected error: %s", exc)
    finally:
        db.close()
    """
    Run postmortem data fetch for all active jobs.
    Fires Saturday at 6:00 AM Mountain Time.

    Fetches Expected All Cost + actual cost per cost code for every active HCSS
    job, stores results in postmortem_job_snapshots + postmortem_cost_code_results,
    and purges any stale (inactive/completed) job rows.
    """
    from api.database import SessionLocal, create_tables
    from api.db_services import purge_stale_postmortem_jobs, store_postmortem_data

    logger.info("Weekly postmortem job starting ...")
    create_tables()
    db = SessionLocal()
    try:
        success, jobs_stored, error = process_postmortem(db)
        if success:
            logger.info("✓ Weekly postmortem complete: %d job(s) stored", jobs_stored)
        else:
            logger.error("Weekly postmortem failed: %s", error)
    except Exception as exc:
        logger.exception("Weekly postmortem job raised an unexpected error: %s", exc)
    finally:
        db.close()


def start_background_scheduler() -> None:
    """
    Start the background scheduler when FastAPI starts.
    Called from the FastAPI lifespan startup event.

    Jobs registered:
    1. unified_sync_job        — every 8 hours  (timelog, quantity, budget)
    2. daily_report_job        — daily at 1 PM Mountain Time
    3. weekly_quantity_job     — Friday at 8 AM Mountain Time
    4. monthly_projection_job  — 23rd each month (projections)
    5. daily_budget_alert_job  — daily at 2 PM Mountain Time
    6. weekly_budget_summary   — Monday at 8 AM Mountain Time
    7. startup_sync            — 2 min after start (one-shot)
    """
    global scheduler

    if scheduler is not None:
        logger.warning("Scheduler already running")
        return

    # Only one worker may run the scheduler — otherwise every job fires once per
    # worker (e.g. the daily email sending 4x with 4 workers).
    if not _acquire_scheduler_lock():
        return

    logger.info("Starting background scheduler...")

    scheduler = BackgroundScheduler(
        timezone="UTC",
        job_defaults={
            "coalesce": True,
            "max_instances": 1,
            "misfire_grace_time": 3600,
        },
    )

    # ── Job 1: 8-hour sync (timelog, quantity, budget) ───────────────────
    scheduler.add_job(
        func=run_unified_sync_job,
        trigger=CronTrigger(hour="*/3", minute=0),
        id="unified_sync_job",
        name="Unified Data Sync Job (Timelog / Quantity / Budget)",
        replace_existing=True,
    )

    # ── Job 1b: HeavyJob portal sync — 12:30 PM Mountain Time ────────────
    # Runs BEFORE the 1 PM email so the slow browser automation never delays
    # the report. Self-gates on PORTAL_AUTOMATION_ENABLED, so it is always
    # scheduled but no-ops until that flag is on (independent of EMAILS_ENABLED).
    scheduler.add_job(
        func=_run_daily_portal_sync,
        trigger=CronTrigger(hour=12, minute=30, second=0, timezone=_MT),
        id="daily_portal_sync_job",
        name="Daily HeavyJob Portal Sync (Approve/Reject)",
        replace_existing=True,
    )

    # ── EMAIL JOBS (CONTROLLED BY ENV) ───────────────────────────────────
    if EMAILS_ENABLED:

        # ── Job 2: Daily time log report — 1:00 PM Mountain Time ─────────
        scheduler.add_job(
            func=_run_daily_report,
            trigger=CronTrigger(hour=13, minute=0, second=0, timezone=_MT),
            id="daily_report_job",
            name="Daily Time Log Report Email",
            replace_existing=True,
        )

        # ── Job 3: Weekly quantity alert — Friday 8:00 AM Mountain Time ──
        scheduler.add_job(
            func=_run_weekly_quantity,
            trigger=CronTrigger(
                day_of_week="fri",
                hour=8,
                minute=0,
                second=0,
                timezone=_MT,
            ),
            id="weekly_quantity_job",
            name="Weekly Quantity Alert Email",
            replace_existing=True,
        )

        # ── Budget Job 4A: Daily alert — 2:00 PM Mountain Time ────────────
        scheduler.add_job(
            func=_run_daily_budget_alert,
            trigger=CronTrigger(hour=14, minute=0, second=0, timezone=_MT),
            id="daily_budget_alert_job",
            name="Daily Budget Alert Email",
            replace_existing=True,
        )

        # ── Budget Job 4B: Weekly summary — Monday 9:30 AM Mountain Time ──
        scheduler.add_job(
            func=_run_weekly_budget_summary,
            trigger=CronTrigger(
                day_of_week="mon",
                hour=9,
                minute=30,
                second=0,
                timezone=_MT,
            ),
            id="weekly_budget_summary_job",
            name="Weekly Budget Summary Email",
            replace_existing=True,
        )

        logger.info("Email jobs ENABLED via EMAILS_ENABLED=true")

    else:
        logger.info("Email jobs DISABLED via EMAILS_ENABLED=false")

    # ── Job 5: Monthly projection — 23rd at 08:00 UTC ────────────────────
    scheduler.add_job(
        func=run_monthly_projection_job,
        trigger=CronTrigger(day=23, hour=8, minute=0),
        id="monthly_projection_job",
        name="Monthly Projection Calculation",
        replace_existing=True,
    )

    # ── Job 5b: Monthly billing draft — 23rd at 03:00 MT (previous month) ──
    scheduler.add_job(
        func=_run_monthly_billing_draft,
        trigger=CronTrigger(day=23, hour=3, minute=0, timezone=_MT),
        id="monthly_billing_draft_job",
        name="Monthly Billing Draft (previous month)",
        replace_existing=True,
    )
    # ── Job 7: Weekly postmortem — Saturday 06:00 AM Mountain Time ───────
    scheduler.add_job(
        func=_run_weekly_postmortem,
        trigger=CronTrigger(
            day_of_week="sat",
            hour=6,
            minute=0,
            second=0,
            timezone=_MT,
        ),
        id="weekly_postmortem_job",
        name="Weekly Postmortem Data Refresh (All Active Jobs)",
        replace_existing=True,
    )

    # ── Job 6: One-shot startup sync (2 min after boot) ───────────────────
    scheduler.add_job(
        func=run_unified_sync_job,
        trigger="date",
        run_date=datetime.now() + timedelta(minutes=2),
        id="startup_sync",
        name="Startup Data Sync",
    )

    scheduler.start()

    logger.info("✓ Background scheduler started successfully")
    logger.info("  - Sync job (timelog/quantity/budget) runs every 3 hours UTC")
    logger.info("  - Monthly projection runs on 23rd of each month at 08:00 UTC")
    logger.info("  - Weekly postmortem runs Saturday at 06:00 AM Mountain Time")
    logger.info("  - Initial sync will run in 2 minutes")

    unified_job = scheduler.get_job("unified_sync_job")
    if unified_job and unified_job.next_run_time:
        logger.info("  - Next unified sync: %s", unified_job.next_run_time)

    projection_job = scheduler.get_job("monthly_projection_job")
    if projection_job and projection_job.next_run_time:
        logger.info("  - Next projection run: %s", projection_job.next_run_time)

    postmortem_job = scheduler.get_job("weekly_postmortem_job")
    if postmortem_job and postmortem_job.next_run_time:
        logger.info("  - Next postmortem run: %s", postmortem_job.next_run_time)


def stop_background_scheduler() -> None:
    """Stop the background scheduler when FastAPI shuts down."""
    global scheduler
    if scheduler is None:
        return
    logger.info("Stopping background scheduler...")
    scheduler.shutdown(wait=True)
    scheduler = None
    logger.info("✓ Background scheduler stopped")


def get_scheduler_status() -> dict:
    """Returns current scheduler status and all job info."""
    if scheduler is None:
        return {"running": False, "jobs": []}

    jobs = [
        {
            "id": job.id,
            "name": job.name,
            "next_run_time": job.next_run_time.isoformat() if job.next_run_time else None,
            "trigger": str(job.trigger),
        }
        for job in scheduler.get_jobs()
    ]
    return {"running": scheduler.running, "jobs": jobs}