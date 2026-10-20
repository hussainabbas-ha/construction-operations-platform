"""
Production cron job monitoring script.
Run this to get a full overview of cron job execution on production.
"""
import sys, json
from pathlib import Path
from datetime import datetime, timezone, timedelta
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from api.database import SessionLocal, CronJobExecution, TimelogVerificationCache, QuantityTrackingCache, BudgetTrackingCache
from sqlalchemy import desc, func

SEP = "=" * 70

def fmt(dt):
    if not dt:
        return "N/A"
    if hasattr(dt, 'tzinfo') and dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.strftime("%Y-%m-%d %H:%M:%S UTC")

db = SessionLocal()
now = datetime.now(timezone.utc)

print(SEP)
print("  PRODUCTION CRON JOB STATUS REPORT")
print(f"  Generated: {fmt(now)}")
print(SEP)

# ── SCHEDULER STATUS ─────────────────────────────────────────────────────────
print("\n📅 SCHEDULER")
try:
    from scheduler.background_scheduler import get_scheduler_status
    sched = get_scheduler_status()
    print(f"  Running : {sched['running']}")
    for job in sched.get('jobs', []):
        print(f"  Job     : {job['name']}")
        print(f"  Next run: {job['next_run_time'] or 'N/A'}")
        print(f"  Trigger : {job['trigger']}")
except Exception as e:
    print(f"  Could not get scheduler status: {e}")

# ── LAST 7 DAYS SUMMARY ──────────────────────────────────────────────────────
print(f"\n📊 LAST 7 DAYS SUMMARY")
since = now - timedelta(days=7)
all_jobs = db.query(CronJobExecution).filter(
    CronJobExecution.created_at >= since
).order_by(desc(CronJobExecution.created_at)).all()

total    = len(all_jobs)
success  = sum(1 for j in all_jobs if j.status == "success")
failed   = sum(1 for j in all_jobs if j.status == "failed")
retrying = sum(1 for j in all_jobs if j.status == "retrying")
running  = sum(1 for j in all_jobs if j.status == "running")

print(f"  Total jobs   : {total}")
print(f"  ✓ Success    : {success}")
print(f"  ✗ Failed     : {failed}")
print(f"  ↻ Retrying   : {retrying}")
print(f"  ⟳ Running    : {running}")
print(f"  Success rate : {round(success/total*100)}%" if total else "  Success rate : N/A")

# ── PER MODULE BREAKDOWN ─────────────────────────────────────────────────────
print(f"\n📦 PER MODULE BREAKDOWN (last 7 days)")
for module in ("timelog", "quantity", "budget"):
    jobs = [j for j in all_jobs if j.job_type == module]
    s = sum(1 for j in jobs if j.status == "success")
    f = sum(1 for j in jobs if j.status == "failed")
    r = sum(1 for j in jobs if j.status == "retrying")
    last = next((j for j in jobs if j.status == "success"), None)
    print(f"\n  [{module.upper()}]")
    print(f"    Runs    : {len(jobs)}  ✓{s}  ✗{f}  ↻{r}")
    if last:
        print(f"    Last OK : {last.execution_date} at {fmt(last.completed_at)} ({last.records_processed} records)")
    else:
        print(f"    Last OK : None in last 7 days")

# ── TODAY'S RUNS ─────────────────────────────────────────────────────────────
today_str = now.strftime("%Y-%m-%d")
today_jobs = [j for j in all_jobs if j.created_at and
              (j.created_at.replace(tzinfo=timezone.utc) if j.created_at.tzinfo is None else j.created_at).strftime("%Y-%m-%d") == today_str]

print(f"\n🕐 TODAY'S RUNS ({today_str})")
if not today_jobs:
    print("  ⚠️  No jobs ran today yet")
    # Check if startup sync should have run
    print("  (Startup sync runs 2 min after app start, then every 8h at 00:00/08:00/16:00 UTC)")
else:
    print(f"  Total: {len(today_jobs)}")
    for j in today_jobs[:30]:
        icon = "✓" if j.status == "success" else ("✗" if j.status == "failed" else "↻")
        records = f"({j.records_processed} records)" if j.records_processed is not None else ""
        print(f"  {icon} {j.job_type:8} | {j.execution_date} | {j.status:8} {records}")

# ── PROBLEM JOBS ─────────────────────────────────────────────────────────────
problem_jobs = [j for j in all_jobs if j.status in ("failed", "retrying")]
print(f"\n⚠️  PROBLEM JOBS ({len(problem_jobs)} total)")
if not problem_jobs:
    print("  None — all good!")
else:
    for j in problem_jobs[:10]:
        print(f"\n  ✗ {j.job_type} | {j.execution_date} | {j.status} | attempt {j.attempt_count}/{j.max_retries}")
        if j.error_message:
            print(f"    Error: {j.error_message[:150]}")
        if j.next_retry_at:
            print(f"    Next retry: {fmt(j.next_retry_at)}")

# ── DATA COVERAGE ─────────────────────────────────────────────────────────────
print(f"\n📅 DATA COVERAGE IN DB")

tl = db.query(func.count(TimelogVerificationCache.id),
              func.min(TimelogVerificationCache.verification_date),
              func.max(TimelogVerificationCache.verification_date)).first()
print(f"  Timelog  : {tl[0]} dates  |  {tl[1]} → {tl[2]}")

qt = db.query(func.count(QuantityTrackingCache.id),
              func.min(QuantityTrackingCache.tracking_date),
              func.max(QuantityTrackingCache.tracking_date)).first()
print(f"  Quantity : {qt[0]} dates  |  {qt[1]} → {qt[2]}")

bt = db.query(func.count(BudgetTrackingCache.id),
              func.min(BudgetTrackingCache.tracking_date),
              func.max(BudgetTrackingCache.tracking_date)).first()
print(f"  Budget   : {bt[0]} dates  |  {bt[1]} → {bt[2]}")

# ── RECENT 10 RUNS ────────────────────────────────────────────────────────────
print(f"\n🕒 MOST RECENT 10 RUNS")
for j in all_jobs[:10]:
    icon = "✓" if j.status == "success" else ("✗" if j.status == "failed" else "↻")
    records = f"| {j.records_processed} records" if j.records_processed is not None else ""
    print(f"  {icon} {j.job_type:8} | {j.execution_date} | {j.status:8} {records} | {fmt(j.completed_at or j.created_at)}")

print(f"\n{SEP}")
db.close()
