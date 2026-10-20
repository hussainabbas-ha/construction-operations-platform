"""
Clears all stuck/pending/retrying/failed cron job records from the local DB.
Run this before doing a manual backfill to start clean.

What it removes:
  - pending   (never started)
  - retrying  (waiting for next retry that may never come locally)
  - failed    (hit max retries)
  - running   (stuck mid-run, app was killed)

What it keeps:
  - success   (already processed correctly — don't touch these)
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from api.database import SessionLocal, CronJobExecution
from sqlalchemy import and_

db = SessionLocal()

STUCK_STATUSES = ["pending", "retrying", "failed", "running"]

# Count before
total_before = db.query(CronJobExecution).count()
stuck = db.query(CronJobExecution).filter(
    CronJobExecution.status.in_(STUCK_STATUSES)
).all()

print("=" * 60)
print("  CLEAR STUCK CRON JOBS")
print("=" * 60)
print(f"\n  Total records in DB : {total_before}")
print(f"  Stuck records found : {len(stuck)}")
print()

# Show breakdown
for status in STUCK_STATUSES:
    count = sum(1 for j in stuck if j.status == status)
    if count:
        print(f"    {status:10} : {count}")

if not stuck:
    print("  Nothing to clear — DB is already clean.")
    db.close()
    exit(0)

print(f"\n  Deleting {len(stuck)} stuck records...")
deleted = db.query(CronJobExecution).filter(
    CronJobExecution.status.in_(STUCK_STATUSES)
).delete(synchronize_session=False)
db.commit()

total_after = db.query(CronJobExecution).count()
success_kept = db.query(CronJobExecution).filter(
    CronJobExecution.status == "success"
).count()

print(f"  ✓ Deleted  : {deleted} records")
print(f"  ✓ Kept     : {success_kept} success records (untouched)")
print(f"  ✓ Remaining: {total_after} total records")
print()
print("  Ready for backfill.")
print("=" * 60)

db.close()
