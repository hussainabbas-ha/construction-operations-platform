from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from payroll_verification.reporting import group_results
from payroll_verification.verifier import verify_payroll_date


def _default_date() -> str:
    return (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")


def main() -> None:
    parser = argparse.ArgumentParser(description="Gould payroll verification (HCSS HeavyJob).")
    parser.add_argument("--date", default=_default_date(), help="Target date in YYYY-MM-DD (default: yesterday).")
    args = parser.parse_args()

    results = verify_payroll_date(target_date=args.date)
    grouped = group_results(results)

    print(f"--- HCSS Payroll Verification Report for {args.date} ---")
    for status in ("REJECTED", "FLAGGED", "APPROVED"):
        items = grouped[status]
        print(f"\n[{status}] - {len(items)} records")
        for r in items:
            if status == "REJECTED":
                print(f"- {r.job_code} | {r.foreman} | {r.id[:8]} (REASON: {', '.join(r.reasons)})")
            elif status == "FLAGGED":
                print(f"- {r.job_code} | {r.foreman} | {r.id[:8]} (FLAGS: {', '.join(r.flags)})")
            else:
                print(f"- {r.job_code} | {r.foreman} | {r.id[:8]}")


if __name__ == "__main__":
    main()

