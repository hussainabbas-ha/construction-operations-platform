"""
verify_multi_costcodes.py
─────────────────────────────────────────────────────────────────────────────
Verify multiple cost codes across ANY job — auto-discovers UUIDs and job IDs.

Steps:
  1. Fetch all active jobs from HCSS
  2. Search costCodes endpoint for each target code string → get UUID + job ID
  3. Fetch v1/jobCostsToDate (cumulative) for each → actual cost
  4. Print comparison table ready to verify against portal

Usage:
  python verify_multi_costcodes.py
  python verify_multi_costcodes.py --codes "1360350018A,1520200010C,1120200220B"
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from dotenv import load_dotenv
load_dotenv()

from payroll_verification.hcss_client import HCSSClient

JOBS_URL          = "https://api.hcssapps.com/heavyjob/api/v1/jobs"
COST_CODES_URL    = "https://api.hcssapps.com/heavyjob/api/v2/costCodes/advancedRequest"
V1_TODATE_URL     = "https://api.hcssapps.com/heavyjob/api/v1/jobCostsToDate/search"

DEFAULT_CODES = [
    "1360350018A",
    "1520200010C",
    "1120200220B",
    "1521321000B",
    "1120200203B",
]


def fetch_all_pages(client, method, url, payload=None, params=None):
    rows, cursor = [], None
    while True:
        if cursor:
            if payload is not None:
                payload["cursor"] = cursor
            else:
                params = dict(params or {})
                params["cursor"] = cursor
        raw = client._request(method, url, json=payload, params=params)
        batch, next_cursor = client._extract_results_and_cursor(raw)
        rows.extend(batch)
        if not next_cursor:
            break
        cursor = next_cursor
    return rows


def hdiv(w=90): print("═" * w)
def div(w=90):  print("─" * w)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--codes",
        default=",".join(DEFAULT_CODES),
        help="Comma-separated cost code strings to verify",
    )
    args = parser.parse_args()
    target_codes = [c.strip() for c in args.codes.split(",") if c.strip()]

    client = HCSSClient()

    hdiv()
    print(f"  MULTI COST CODE VERIFICATION")
    print(f"  Target codes: {', '.join(target_codes)}")
    hdiv()

    # ── Step 1: Get all active jobs ───────────────────────────────────────────
    print("\n[Step 1] Fetching all active jobs...")
    raw_jobs = client._request("GET", JOBS_URL)
    if isinstance(raw_jobs, list):
        all_jobs = raw_jobs
    else:
        all_jobs = raw_jobs.get("results", []) or []

    # Filter to active/open jobs only
    active_jobs = [
        j for j in all_jobs
        if not j.get("isDeleted", False)
        and j.get("status", "").lower() not in ("closed", "complete", "completed")
    ]
    all_job_ids = [j.get("id") for j in active_jobs if j.get("id")]
    job_code_map = {j.get("id"): j.get("code", "?") for j in active_jobs if j.get("id")}

    print(f"  Total jobs: {len(all_jobs)}  Active: {len(active_jobs)}")

    # ── Step 2: Search costCodes across all active jobs ───────────────────────
    print(f"\n[Step 2] Searching cost codes across {len(active_jobs)} active jobs...")
    print("  (This may take a moment — batching in groups of 50 jobs)")

    # Batch job IDs in groups of 50 to avoid huge requests
    BATCH = 50
    found: dict[str, dict] = {}  # code_string → { id, jobId, jobCode, description, budget }

    for i in range(0, len(all_job_ids), BATCH):
        batch_ids = all_job_ids[i: i + BATCH]
        cc_rows = fetch_all_pages(client, "POST", COST_CODES_URL, payload={
            "jobIds": batch_ids,
            "limit":  500,
        })
        for cc in cc_rows:
            code_str = cc.get("code") or cc.get("costCodeCode") or ""
            if code_str in target_codes and code_str not in found:
                labor_b = float(cc.get("laborDollars")       or 0)
                equip_b = float(cc.get("equipmentDollars")   or 0)
                mat_b   = float(cc.get("materialDollars")    or 0)
                sub_b   = float(cc.get("subcontractDollars") or 0)
                found[code_str] = {
                    "cc_id":       cc.get("id", ""),
                    "job_id":      cc.get("jobId", ""),
                    "job_code":    cc.get("jobCode") or job_code_map.get(cc.get("jobId"), "?"),
                    "description": cc.get("description") or cc.get("costCodeDescription", ""),
                    "labor_bud":   labor_b,
                    "equip_bud":   equip_b,
                    "mat_bud":     mat_b,
                    "sub_bud":     sub_b,
                    "total_bud":   labor_b + equip_b + mat_b + sub_b,
                    "planned_qty": float(cc.get("quantity") or 0),
                    "uom":         cc.get("unitOfMeasure") or "",
                }
        if len(found) == len(target_codes):
            break  # found all, stop early

    not_found = [c for c in target_codes if c not in found]
    print(f"  Found: {len(found)}/{len(target_codes)}")
    if not_found:
        print(f"  ✗ NOT FOUND in active jobs: {not_found}")
        print("  These may be on closed/completed jobs or spelled differently.")

    if not found:
        print("\n  No cost codes found. Check spelling or job status.")
        sys.exit(0)

    # ── Step 3: Fetch cumulative actuals from v1/jobCostsToDate ──────────────
    print(f"\n[Step 3] Fetching cumulative actuals (v1/jobCostsToDate) for {len(found)} cost codes...")

    # Group by job_id to minimise API calls
    job_to_codes: dict[str, list[str]] = {}
    for code_str, info in found.items():
        job_to_codes.setdefault(info["job_id"], []).append(code_str)

    # Fetch per job
    actual_map: dict[str, float] = {}  # cc_id → cumulative actual cost

    for job_id, codes_in_job in job_to_codes.items():
        cc_ids = [found[c]["cc_id"] for c in codes_in_job]
        rows = fetch_all_pages(client, "POST", V1_TODATE_URL, payload={
            "jobIds":      [job_id],
            "costCodeIds": cc_ids,
            "limit":       500,
        })
        # Aggregate by cost code id
        for row in rows:
            ccid = (row.get("costCode") or {}).get("costCodeId", "")
            labor = float(row.get("totalLaborCost")       or row.get("laborCost")       or 0)
            equip = float(row.get("totalEquipmentCost")   or row.get("equipmentCost")   or 0)
            mat   = float(row.get("totalMaterialCost")    or row.get("materialCost")    or 0)
            sub   = float(row.get("totalSubcontractCost") or row.get("subcontractCost") or 0)
            truck = float(row.get("totalTruckingCost")    or row.get("truckingCost")    or 0)
            actual_map[ccid] = actual_map.get(ccid, 0.0) + labor + equip + mat + sub + truck

    # ── Step 4: Print results table ───────────────────────────────────────────
    hdiv()
    print("  RESULTS — compare against HCSS portal (no date filter = cumulative)")
    hdiv()
    print(f"  {'Code':<18} {'Job':<8} {'Description':<35} {'Budget':>12} {'Actual':>12} {'Util%':>7} {'Status':<10}")
    div()

    results = []
    for code_str in target_codes:
        if code_str not in found:
            print(f"  {code_str:<18} {'N/A':<8} {'NOT FOUND IN ACTIVE JOBS':<35}")
            continue

        info       = found[code_str]
        cc_id      = info["cc_id"]
        actual     = actual_map.get(cc_id, 0.0)
        budget     = info["total_bud"]
        util       = round((actual / budget) * 100, 1) if budget > 0 else None
        util_str   = f"{util}%" if util is not None else "N/A"
        variance   = budget - actual

        if budget <= 0:
            status = "OVER_RISK" if actual > 0 else "ON_TRACK"
        elif util >= 75:
            status = "OVER_RISK"
        else:
            status = "ON_TRACK"

        desc = info["description"][:35]
        print(
            f"  {code_str:<18} {info['job_code']:<8} {desc:<35} "
            f"${budget:>11,.2f} ${actual:>11,.2f} {util_str:>7}  {status}"
        )

        results.append({
            "cost_code":      code_str,
            "cost_code_id":   cc_id,
            "job_id":         info["job_id"],
            "job_code":       info["job_code"],
            "description":    info["description"],
            "planned_budget": round(budget, 2),
            "actual_cost":    round(actual, 2),
            "utilization_pct": util,
            "variance":       round(variance, 2),
            "status":         status,
            "budget_breakdown": {
                "labor":      info["labor_bud"],
                "equipment":  info["equip_bud"],
                "material":   info["mat_bud"],
                "subcontract":info["sub_bud"],
            },
        })

    div()
    total_budget = sum(r["planned_budget"] for r in results)
    total_actual = sum(r["actual_cost"]    for r in results)
    overall_util = round((total_actual / total_budget * 100), 1) if total_budget > 0 else 0
    print(f"  {'TOTALS':<18} {'':8} {'':35} ${total_budget:>11,.2f} ${total_actual:>11,.2f} {overall_util}%")

    # ── Save output ───────────────────────────────────────────────────────────
    out = Path(__file__).parent / "verify_multi_output.json"
    out.write_text(json.dumps({
        "note": "actual_cost = cumulative to date (v1/jobCostsToDate). Compare to portal with NO date filter.",
        "results": results,
    }, indent=2))

    hdiv()
    print(f"\n  Full output → verify_multi_output.json")
    print("\n  HOW TO VERIFY IN PORTAL:")
    print("    Remove any date filter in HCSS portal (show all time)")
    print("    'Actual All Cost' column should match our actual_cost")
    print("    'Budget' column should match our planned_budget")
    print()


if __name__ == "__main__":
    main()
