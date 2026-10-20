"""
verify_quantity_fix.py
========================
Run AFTER deploying the fix to confirm quantity data matches HCSS.

Compares three data sources for job 26004:
  1. HCSS /costCodes/advancedRequest   → budgeted quantity (planned)
  2. HCSS /costCode/progress            → toDateQuantity (actual installed)
  3. Your API /api/quantity/verify       → what your dashboard shows

If all three align, the fix is working.

Usage:
  python verify_quantity_fix.py
  python verify_quantity_fix.py --api-url http://localhost:8000
  python verify_quantity_fix.py --job-id <UUID>
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from dotenv import load_dotenv
load_dotenv(REPO_ROOT / ".env")

# ── Terminal colors ───────────────────────────────────────────────────────────
GREEN  = "\033[92m"
RED    = "\033[91m"
YELLOW = "\033[93m"
CYAN   = "\033[96m"
BOLD   = "\033[1m"
RESET  = "\033[0m"

TARGET_JOB_ID = "4e94268c-5630-4b7a-bba6-8000efbb65e1"
TARGET_JOB_CODE = "26004"


def main():
    parser = argparse.ArgumentParser(description="Verify quantity fix against HCSS")
    parser.add_argument("--api-url", default="http://localhost:8000", help="Your API base URL")
    parser.add_argument("--job-id", default=TARGET_JOB_ID, help="HCSS job UUID to check")
    args = parser.parse_args()

    print(f"\n{BOLD}{'=' * 70}")
    print("QUANTITY FIX VERIFICATION")
    print(f"{'=' * 70}{RESET}")
    print(f"  Job: {TARGET_JOB_CODE} ({args.job_id[:12]}...)")
    print(f"  API: {args.api_url}")
    print()

    # ── Step 1: Get truth from HCSS directly ──────────────────────────────
    print(f"{BOLD}Step 1: Fetching ground truth from HCSS API...{RESET}")

    from payroll_verification.hcss_client import HCSSClient
    client = HCSSClient()

    # Get budgeted quantities
    cc_raw = client._request(
        "POST",
        "https://api.hcssapps.com/heavyjob/api/v2/costCodes/advancedRequest",
        json={"jobIds": [args.job_id], "limit": 500},
    )
    cost_codes = cc_raw.get("results", []) if isinstance(cc_raw, dict) else cc_raw or []
    cc_budget_map = {}  # cc_id → {code, description, quantity, unit}
    for cc in cost_codes:
        ccid = cc.get("id")
        if ccid and cc.get("quantity", 0) > 0:
            cc_budget_map[ccid] = {
                "code": cc.get("code", ""),
                "description": cc.get("description", ""),
                "budgeted_qty": cc.get("quantity", 0),
                "unit": cc.get("unitOfMeasure", ""),
            }
    print(f"  Found {len(cc_budget_map)} cost codes with budget > 0")

    # Get actual quantities (cumulative)
    progress_raw = client._request(
        "POST",
        f"{client.heavyjob_base}/costCode/progress/advancedRequest",
        json={"jobId": args.job_id, "limit": 500},
    )
    progress_list = progress_raw.get("results", []) if isinstance(progress_raw, dict) else progress_raw or []

    # Take max toDateQuantity per cost code (most recent cumulative)
    hcss_actual_map = {}  # cc_id → toDateQuantity
    for p in progress_list:
        ccid = p.get("costCodeId")
        qty = float(p.get("toDateQuantity", 0) or 0)
        if ccid:
            hcss_actual_map[ccid] = max(hcss_actual_map.get(ccid, 0.0), qty)
    print(f"  Found {len(hcss_actual_map)} cost codes with progress data")

    # ── Step 2: Get data from your API ────────────────────────────────────
    print(f"\n{BOLD}Step 2: Fetching from your API ({args.api_url})...{RESET}")

    import requests
    try:
        from datetime import date, timedelta
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        resp = requests.get(
            f"{args.api_url}/api/quantity/verify",
            params={"date": yesterday},
            timeout=30,
        )
        if resp.status_code == 404:
            print(f"  {YELLOW}⚠ API returned 404 — no data cached for {yesterday}")
            print(f"    Run the cron job first: python -m scheduler.unified_cron_job{RESET}")
            api_items = []
        elif resp.status_code != 200:
            print(f"  {RED}✗ API returned HTTP {resp.status_code}{RESET}")
            api_items = []
        else:
            data = resp.json()
            api_items = data.get("items", [])
            print(f"  Got {len(api_items)} items from API")
    except requests.ConnectionError:
        print(f"  {RED}✗ Cannot connect to {args.api_url} — is the server running?{RESET}")
        api_items = []
    except Exception as exc:
        print(f"  {RED}✗ API error: {exc}{RESET}")
        api_items = []

    # Build API lookup
    api_map = {}  # cc_id → {installed_quantity, planned_quantity, percent_complete}
    for item in api_items:
        api_map[item.get("cost_code_id", "")] = {
            "installed": item.get("installed_quantity", 0),
            "planned": item.get("planned_quantity", 0),
            "pct": item.get("percent_complete", 0),
            "status": item.get("status", ""),
        }

    # ── Step 3: Compare ──────────────────────────────────────────────────
    print(f"\n{BOLD}Step 3: Comparing values...{RESET}")
    print()

    # Header
    header = f"{'Cost Code':<14} {'Description':<35} {'Unit':<6} {'Budget':>8} {'HCSS Actual':>12} {'API Actual':>11} {'Match':>6}"
    print(f"  {BOLD}{header}{RESET}")
    print(f"  {'─' * len(header)}")

    total_checked = 0
    total_match = 0
    total_mismatch = 0
    mismatches = []

    # Sort by code for readability
    sorted_ids = sorted(
        cc_budget_map.keys(),
        key=lambda x: cc_budget_map[x]["code"],
    )

    for ccid in sorted_ids:
        cc = cc_budget_map[ccid]
        hcss_actual = hcss_actual_map.get(ccid, 0.0)
        api_data = api_map.get(ccid)

        if api_data is None:
            api_actual = None
            api_str = "N/A"
            match_str = f"{YELLOW}?{RESET}"
        else:
            api_actual = api_data["installed"]
            api_str = f"{api_actual:>11,.2f}"

            # Check if they match (within 0.1 tolerance for rounding)
            if abs(api_actual - hcss_actual) < 0.1:
                match_str = f"{GREEN}✓{RESET}"
                total_match += 1
            else:
                match_str = f"{RED}✗{RESET}"
                total_mismatch += 1
                mismatches.append({
                    "code": cc["code"],
                    "description": cc["description"],
                    "hcss": hcss_actual,
                    "api": api_actual,
                    "diff": api_actual - hcss_actual,
                })

        total_checked += 1

        # Only print the first 30 + all mismatches for readability
        if total_checked <= 30 or (api_data and abs((api_data["installed"] if api_data else 0) - hcss_actual) >= 0.1):
            desc = cc["description"][:33]
            print(
                f"  {cc['code']:<14} {desc:<35} {cc['unit']:<6} "
                f"{cc['budgeted_qty']:>8,.1f} {hcss_actual:>12,.2f} "
                f"{api_str:>11} {match_str:>6}"
            )

    if total_checked > 30:
        print(f"  ... and {total_checked - 30} more cost codes")

    # ── Summary ───────────────────────────────────────────────────────────
    print(f"\n{BOLD}{'=' * 70}")
    print(f"RESULTS{RESET}")
    print(f"  Total cost codes checked: {total_checked}")
    print(f"  {GREEN}Matching:   {total_match}{RESET}")
    print(f"  {RED}Mismatched: {total_mismatch}{RESET}")

    if not api_map:
        print(f"\n  {YELLOW}⚠ No API data available — run the cron job first:")
        print(f"    python -m scheduler.unified_cron_job{RESET}")
    elif total_mismatch == 0:
        print(f"\n  {GREEN}{BOLD}✓ ALL VALUES MATCH HCSS — fix is working correctly!{RESET}")
    else:
        print(f"\n  {RED}{BOLD}✗ {total_mismatch} MISMATCHES FOUND:{RESET}")
        for m in mismatches[:10]:
            print(
                f"    {m['code']}: HCSS={m['hcss']:.2f}  API={m['api']:.2f}  "
                f"diff={m['diff']:+.2f}"
            )
        if len(mismatches) > 10:
            print(f"    ... and {len(mismatches) - 10} more")
        print(f"\n  {YELLOW}Did you clear the old cache? Run: python clear_quantity_cache.py{RESET}")

    print()


if __name__ == "__main__":
    main()