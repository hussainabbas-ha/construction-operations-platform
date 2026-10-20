"""
Quick script to check if the cron job is running and what data exists.
Run this to diagnose the issue.
"""
import os
import sys
from datetime import date, timedelta
from pathlib import Path

# Add repo root to path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv
load_dotenv()

from api.database import SessionLocal
from api.database import (
    TimelogVerificationCache,
    QuantityTrackingCache,
    BudgetTrackingCache,
    CronJobExecution,
)
from sqlalchemy import func, desc

def check_status():
    db = SessionLocal()
    
    print("=" * 80)
    print("CRON JOB STATUS CHECK")
    print("=" * 80)
    print()
    
    # Current date info
    today = date.today()
    yesterday = today - timedelta(days=1)
    print(f"Today: {today}")
    print(f"Yesterday: {yesterday}")
    print()
    
    # Expected dates to be processed
    print("Expected dates in database (yesterday - 7 days):")
    for i in range(7):
        target_date = yesterday - timedelta(days=i)
        print(f"  - {target_date}")
    print()
    
    # Check timelog data
    print("-" * 80)
    print("TIMELOG VERIFICATION DATA")
    print("-" * 80)
    timelog_count = db.query(TimelogVerificationCache).count()
    print(f"Total cache entries: {timelog_count}")
    
    if timelog_count > 0:
        oldest = db.query(TimelogVerificationCache).order_by(
            TimelogVerificationCache.verification_date.asc()
        ).first()
        newest = db.query(TimelogVerificationCache).order_by(
            TimelogVerificationCache.verification_date.desc()
        ).first()
        print(f"Oldest date: {oldest.verification_date}")
        print(f"Newest date: {newest.verification_date}")
        print(f"Last updated: {newest.updated_at}")
        
        # Check recent dates
        print("\nRecent dates:")
        recent = db.query(TimelogVerificationCache).order_by(
            TimelogVerificationCache.verification_date.desc()
        ).limit(10).all()
        for entry in recent:
            print(f"  {entry.verification_date}: {entry.total_timecards} timecards (updated: {entry.updated_at})")
    else:
        print("⚠️  NO DATA FOUND")
    print()
    
    # Check quantity data
    print("-" * 80)
    print("QUANTITY TRACKING DATA")
    print("-" * 80)
    quantity_count = db.query(QuantityTrackingCache).count()
    print(f"Total cache entries: {quantity_count}")
    
    if quantity_count > 0:
        oldest = db.query(QuantityTrackingCache).order_by(
            QuantityTrackingCache.tracking_date.asc()
        ).first()
        newest = db.query(QuantityTrackingCache).order_by(
            QuantityTrackingCache.tracking_date.desc()
        ).first()
        print(f"Oldest date: {oldest.tracking_date}")
        print(f"Newest date: {newest.tracking_date}")
        print(f"Last updated: {newest.updated_at}")
        
        # Check recent dates
        print("\nRecent dates:")
        recent = db.query(QuantityTrackingCache).order_by(
            QuantityTrackingCache.tracking_date.desc()
        ).limit(10).all()
        for entry in recent:
            print(f"  {entry.tracking_date}: {entry.total_cost_codes} cost codes (updated: {entry.updated_at})")
    else:
        print("⚠️  NO DATA FOUND")
    print()
    
    # Check budget data
    print("-" * 80)
    print("BUDGET TRACKING DATA")
    print("-" * 80)
    budget_count = db.query(BudgetTrackingCache).count()
    print(f"Total cache entries: {budget_count}")
    
    if budget_count > 0:
        oldest = db.query(BudgetTrackingCache).order_by(
            BudgetTrackingCache.tracking_date.asc()
        ).first()
        newest = db.query(BudgetTrackingCache).order_by(
            BudgetTrackingCache.tracking_date.desc()
        ).first()
        print(f"Oldest date: {oldest.tracking_date}")
        print(f"Newest date: {newest.tracking_date}")
        print(f"Last updated: {newest.updated_at}")
        
        # Check recent dates
        print("\nRecent dates:")
        recent = db.query(BudgetTrackingCache).order_by(
            BudgetTrackingCache.tracking_date.desc()
        ).limit(10).all()
        for entry in recent:
            print(f"  {entry.tracking_date}: {entry.total_cost_codes} cost codes (updated: {entry.updated_at})")
    else:
        print("⚠️  NO DATA FOUND")
    print()
    
    # Check cron job executions
    print("-" * 80)
    print("CRON JOB EXECUTIONS")
    print("-" * 80)
    total_jobs = db.query(CronJobExecution).count()
    print(f"Total job executions: {total_jobs}")
    
    if total_jobs > 0:
        # Status breakdown
        print("\nStatus breakdown:")
        statuses = db.query(
            CronJobExecution.status,
            func.count(CronJobExecution.id)
        ).group_by(CronJobExecution.status).all()
        for status, count in statuses:
            print(f"  {status}: {count}")
        
        # Recent executions
        print("\nRecent executions (last 20):")
        recent_jobs = db.query(CronJobExecution).order_by(
            desc(CronJobExecution.created_at)
        ).limit(20).all()
        
        for job in recent_jobs:
            status_icon = "✓" if job.status == "success" else "✗" if job.status == "failed" else "⟳"
            print(f"  {status_icon} {job.job_type:10} | {job.execution_date} | {job.status:10} | "
                  f"attempt {job.attempt_count}/{job.max_retries} | "
                  f"records: {job.records_processed or 0:4} | "
                  f"{job.created_at}")
            if job.error_message:
                print(f"     Error: {job.error_message[:100]}")
        
        # Failed/retrying jobs
        failed_jobs = db.query(CronJobExecution).filter(
            CronJobExecution.status.in_(['failed', 'retrying'])
        ).order_by(desc(CronJobExecution.created_at)).limit(10).all()
        
        if failed_jobs:
            print("\n⚠️  FAILED/RETRYING JOBS:")
            for job in failed_jobs:
                print(f"  ✗ {job.job_type} | {job.execution_date} | {job.status} | "
                      f"attempt {job.attempt_count}/{job.max_retries}")
                if job.error_message:
                    print(f"     Error: {job.error_message}")
                if job.next_retry_at:
                    print(f"     Next retry: {job.next_retry_at}")
        
        # Check if jobs ran today
        print(f"\nJobs created today ({today}):")
        today_jobs = db.query(CronJobExecution).filter(
            func.date(CronJobExecution.created_at) == today
        ).count()
        print(f"  Total: {today_jobs}")
        
        if today_jobs == 0:
            print("  ⚠️  NO JOBS RAN TODAY - CRON JOB MAY NOT BE RUNNING!")
        
        # Check if jobs ran yesterday
        print(f"\nJobs created yesterday ({yesterday}):")
        yesterday_jobs = db.query(CronJobExecution).filter(
            func.date(CronJobExecution.created_at) == yesterday
        ).count()
        print(f"  Total: {yesterday_jobs}")
        
    else:
        print("⚠️  NO CRON JOB EXECUTIONS FOUND - CRON JOB HAS NEVER RUN!")
    
    print()
    print("=" * 80)
    print("DIAGNOSIS")
    print("=" * 80)
    
    if total_jobs == 0:
        print("❌ PROBLEM: Cron job has never executed")
        print("   Possible causes:")
        print("   1. FastAPI application not running")
        print("   2. Scheduler failed to start")
        print("   3. Database connection issue")
        print("   4. Check FastAPI logs for errors")
    elif today_jobs == 0 and yesterday_jobs == 0:
        print("❌ PROBLEM: Cron job hasn't run in the last 2 days")
        print("   Possible causes:")
        print("   1. FastAPI application was restarted and scheduler didn't start")
        print("   2. Scheduler crashed")
        print("   3. Check FastAPI logs for errors")
    elif newest.verification_date < yesterday.isoformat() if timelog_count > 0 else True:
        print("⚠️  WARNING: Data is not up to date")
        print(f"   Expected newest date: {yesterday}")
        print(f"   Actual newest date: {newest.verification_date if timelog_count > 0 else 'N/A'}")
        print("   The cron job may be running but failing to process recent dates")
    else:
        print("✓ Cron job appears to be working")
        print(f"  Latest data: {newest.verification_date if timelog_count > 0 else 'N/A'}")
        print(f"  Note: Today's data ({today}) will be processed tomorrow")
        print(f"        Yesterday's data ({yesterday}) should be available now")
    
    print()
    
    db.close()

if __name__ == "__main__":
    try:
        check_status()
    except Exception as e:
        print(f"❌ ERROR: {e}")
        import traceback
        traceback.print_exc()
