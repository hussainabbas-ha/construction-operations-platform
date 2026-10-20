"""
scripts/clear_postmortem_data.py
─────────────────────────────────
Wipes ALL rows from both postmortem tables so backfill starts clean.

Usage:
    python scripts/clear_postmortem_data.py
"""
from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from dotenv import load_dotenv
load_dotenv(_REPO_ROOT / ".env")

from sqlalchemy import text
from api.database import engine


def main() -> None:
    with engine.connect() as conn:
        # Count before
        cc_count  = conn.execute(text("SELECT COUNT(*) FROM postmortem_cost_code_results")).scalar()
        job_count = conn.execute(text("SELECT COUNT(*) FROM postmortem_job_snapshots")).scalar()

    print(f"\n  Found {job_count} job snapshot(s) and {cc_count} cost code row(s).")

    if job_count == 0 and cc_count == 0:
        print("  Tables are already empty.\n")
        return

    confirm = input(f"\n  Delete ALL {job_count} jobs and {cc_count} cost code rows? [y/N]: ")
    if confirm.strip().lower() != "y":
        print("  Aborted.\n")
        return

    with engine.begin() as conn:
        # Delete child rows first to avoid FK violations, then parent
        cc_deleted  = conn.execute(text("DELETE FROM postmortem_cost_code_results")).rowcount
        job_deleted = conn.execute(text("DELETE FROM postmortem_job_snapshots")).rowcount

    print(f"  ✓ Deleted {job_deleted} job snapshot(s) and {cc_deleted} cost code row(s).")
    print("\n  Now run:  python scripts/backfill_postmortem.py\n")


if __name__ == "__main__":
    main()
