"""
test_hybrid_budget.py
─────────────────────────────────────────────────────────────────────────────
Tests the HYBRID budget approach that gives both:
  - Date filtering (like the original logic)
  - All cost types including material, subcontract, PO (not just timecards)

4 HCSS API calls for actual costs:
  Call 1: POST /v1/jobCosts/advancedRequest                          → labor + equipment (timecards)
  Call 2: POST /v1/costTypes/materialInstalled/advancedRequest       → material costs
  Call 3: POST /v1/costTypes/subcontractWork/advancedRequest         → subcontract costs
  Call 4: POST /v2/costCodes/advancedRequest                         → planned budgets

All 4 use the SAME date window and job IDs.
Results merged by (job_id, cost_code_id) → actual_cost = sum of all types.

USAGE:
  python scripts/test_hybrid_budget.py
  python scripts/test_hybrid_budget.py --start 2026-06-01 --end 2026-06-07
  python scripts/test_hybrid_budget.py --start 2026-05-26 --end 2026-06-01 --top 20

OUTPUT:
  scripts/test_hybrid_budget_output.json  — compare with HCSS portal

HOW TO VERIFY:
  In HCSS portal, set the same date filter (start→end) and compare:
    actual_cost      ← portal "Actual All Cost" (with date filter applied)
    expected_budget  ← portal "Budget" column
    utilization_pct  ← portal "Cost %" column
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any

# Bootstrap path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dotenv import load_dotenv
load_dotenv()

from payroll_verification.hcss_client import HCSSClient

# ── Endpoints ─────────────────────────────────────────────────────────────────
TIMECARDS_URL    = "https://api.hcssapps.com/heavyjob/api/v1/timeCardInfo"
JOBCOSTS_URL     = "https://api.hcssapps.com/heavyjob/api/v1/jobCosts/advancedRequest"
MATERIAL_URL     = "https://api.hcssapps.com/heavyjob/api/v1/costTypes/materialInstalled/advancedRequest"
SUBCONTRACT_URL  = "https://api.hcssapps.com/heavyjob/api/v1/costTypes/subcontractWork/advancedRequest"
COST_CODES_URL   = "https://api.hcssapps.com/heavyjob/api/v2/costCodes/advancedRequest"
JOBS_URL         = "https://api.hcssapps.com/heavyjob/api/v1/jobs"


def fetch_pages(client: HCSSClient, method: str, url: str,
                payload: dict | None = None, params: dict | None = None) -> list[dict]:
    rows, cursor = [], None
    page = 0
    while True:
        if cursor:
            if payload is not None:
                payload = dict(payload)
                payload["cursor"] = cursor
            else:
                params = dict(params or {})
                params["cursor"] = cursor
        raw = client._request(method, url, json=payload, params=params)
        batch, next_cursor = client._extract_results_and_cursor(raw)
        rows.extend(batch)
        page += 1
        if not next_cursor:
            break
        cursor = next_cursor
    return rows


def get_active_job_ids(client: HCSSClient, start: str, end: str) -> list[str]:
    """Get job IDs that had timecard activity in the window — same scope as original logic."""
    print(f"\n[Step 1] Finding active jobs via timecards ({start} → {end})...")
    tcs = fetch_pages(client, "GET", TIMECARDS_URL, params={"startDate": start, "endDate": end})
    job_ids: dict[str, str] = {}
    for tc in tcs:
        jid   = tc.get("jobId") or ""
        jcode = tc.get("jobCode") or tc.get("jobDescription") or "?"
        if jid:
            job_ids[jid] = jcode
    print(f"  {len(tcs)} timecards → {len(job_ids)} active jobs")
    return list(job_ids.keys())


def fetch_labor_equipment(client: HCSSClient, job_ids: list[str],
                          start: str, end: str) -> dict[str, dict]:
    """Call 1: v1/jobCosts — labor + equipment + material + subcontract (all types, date-filtered).

    v1/jobCosts flat response fields:
      costCodeId, foremanId, date, quantity,
      laborCost, laborHours, equipmentCost, equipmentHours,
      materialCost, subcontractCost, truckingCost
    """
    print(f"\n[Call 1] POST /v1/jobCosts/advancedRequest  ({start} → {end})")
    rows = fetch_pages(client, "POST", JOBCOSTS_URL, payload={
        "jobIds":    job_ids,
        "startDate": start,
        "endDate":   end,
        "limit":     500,
    })
    print(f"  {len(rows)} rows returned")
    if rows:
        print(f"  Response field names: {list(rows[0].keys())}")

    # v1/jobCosts is FLAT — no nested job/costCode objects
    # We need to build a ccid → job_id map from the cost code budgets later.
    # For now, key by costCodeId only (we'll enrich with job info from budget map).
    agg: dict[str, dict] = {}
    for row in rows:
        ccid = row.get("costCodeId") or ""
        if not ccid:
            continue

        labor  = float(row.get("laborCost")       or 0)
        equip  = float(row.get("equipmentCost")   or 0)
        mat    = float(row.get("materialCost")     or 0)
        sub    = float(row.get("subcontractCost")  or 0)
        truck  = float(row.get("truckingCost")     or 0)
        hours  = float(row.get("laborHours")       or 0)

        if ccid in agg:
            agg[ccid]["labor_cost"]  += labor
            agg[ccid]["equip_cost"]  += equip
            agg[ccid]["mat_cost"]    += mat
            agg[ccid]["sub_cost"]    += sub + truck
            agg[ccid]["labor_hours"] += hours
        else:
            agg[ccid] = {
                "cc_id":       ccid,
                "cc_code":     "",   # filled from budget map
                "cc_desc":     "",
                "job_id":      "",   # filled from budget map
                "job_code":    "",
                "labor_cost":  labor,
                "equip_cost":  equip,
                "mat_cost":    mat,
                "sub_cost":    sub + truck,
                "labor_hours": hours,
            }

    nonzero = sum(1 for v in agg.values()
                  if v["labor_cost"] + v["equip_cost"] + v["mat_cost"] + v["sub_cost"] > 0)
    print(f"  {len(agg)} unique cost codes ({nonzero} with any cost > 0)")
    return agg


def fetch_material_costs(client: HCSSClient, job_ids: list[str],
                         start: str, end: str, agg: dict[str, dict]) -> None:
    """Call 2: materialInstalled — material costs (date-filtered)."""
    print(f"\n[Call 2] POST /v1/costTypes/materialInstalled/advancedRequest  ({start} → {end})")
    rows = fetch_pages(client, "POST", MATERIAL_URL, payload={
        "jobIds":    job_ids,
        "startDate": start,
        "endDate":   end,
        "limit":     500,
    })
    print(f"  {len(rows)} rows returned")

    added = 0
    for row in rows:
        # Material rows have different structure — check all possible fields
        cc_info  = row.get("costCode") or row.get("costcode") or {}
        job_info = row.get("job") or {}

        ccid = cc_info.get("costCodeId") or cc_info.get("id") or row.get("costCodeId") or ""
        jid  = (job_info.get("jobId")
                or row.get("jobId")
                or row.get("job_id") or "")

        if not ccid or not jid:
            continue

        # Material cost field names vary — try all
        mat_cost = float(
            row.get("totalCost")
            or row.get("cost")
            or row.get("materialCost")
            or row.get("totalMaterialCost")
            or row.get("extendedCost")
            or row.get("unitCost", 0) * float(row.get("quantity") or row.get("installedQuantity") or 0)
            or 0
        )

        key = f"{jid}|{ccid}"
        if key in agg:
            agg[key]["mat_cost"] += mat_cost
        else:
            cc_code = (cc_info.get("costCodeCode") or cc_info.get("code")
                       or row.get("costCodeCode") or "")
            cc_desc = (cc_info.get("costCodeDescription") or cc_info.get("description")
                       or row.get("costCodeDescription") or "")
            agg[key] = {
                "job_id":      jid,
                "job_code":    job_info.get("jobCode") or row.get("jobCode") or "",
                "job_desc":    job_info.get("jobDescription") or "",
                "cc_id":       ccid,
                "cc_code":     cc_code,
                "cc_desc":     cc_desc,
                "labor_cost":  0.0,
                "equip_cost":  0.0,
                "mat_cost":    mat_cost,
                "sub_cost":    0.0,
                "labor_hours": 0.0,
            }
            added += 1

    mat_nonzero = sum(1 for v in agg.values() if v["mat_cost"] > 0)
    print(f"  {mat_nonzero} cost codes now have material costs ({added} new cost codes added)")

    # Debug: show raw fields of first material row
    if rows:
        print(f"  Sample material row keys: {list(rows[0].keys())}")


def fetch_subcontract_costs(client: HCSSClient, job_ids: list[str],
                            start: str, end: str, agg: dict[str, dict]) -> None:
    """Call 3: subcontractWork — subcontract costs (date-filtered)."""
    print(f"\n[Call 3] POST /v1/costTypes/subcontractWork/advancedRequest  ({start} → {end})")
    rows = fetch_pages(client, "POST", SUBCONTRACT_URL, payload={
        "jobIds":    job_ids,
        "startDate": start,
        "endDate":   end,
        "limit":     500,
    })
    print(f"  {len(rows)} rows returned")

    added = 0
    for row in rows:
        cc_info  = row.get("costCode") or row.get("costcode") or {}
        job_info = row.get("job") or {}

        ccid = cc_info.get("costCodeId") or cc_info.get("id") or row.get("costCodeId") or ""
        jid  = (job_info.get("jobId")
                or row.get("jobId")
                or row.get("job_id") or "")

        if not ccid or not jid:
            continue

        sub_cost = float(
            row.get("totalCost")
            or row.get("cost")
            or row.get("subcontractCost")
            or row.get("totalSubcontractCost")
            or row.get("extendedCost")
            or 0
        )

        key = f"{jid}|{ccid}"
        if key in agg:
            agg[key]["sub_cost"] += sub_cost
        else:
            cc_code = (cc_info.get("costCodeCode") or cc_info.get("code")
                       or row.get("costCodeCode") or "")
            cc_desc = (cc_info.get("costCodeDescription") or cc_info.get("description")
                       or row.get("costCodeDescription") or "")
            agg[key] = {
                "job_id":      jid,
                "job_code":    job_info.get("jobCode") or row.get("jobCode") or "",
                "job_desc":    job_info.get("jobDescription") or "",
                "cc_id":       ccid,
                "cc_code":     cc_code,
                "cc_desc":     cc_desc,
                "labor_cost":  0.0,
                "equip_cost":  0.0,
                "mat_cost":    0.0,
                "sub_cost":    sub_cost,
                "labor_hours": 0.0,
            }
            added += 1

    sub_nonzero = sum(1 for v in agg.values() if v["sub_cost"] > 0)
    print(f"  {sub_nonzero} cost codes now have subcontract costs ({added} new cost codes added)")

    if rows:
        print(f"  Sample subcontract row keys: {list(rows[0].keys())}")


def fetch_budgets(client: HCSSClient, job_ids: list[str]) -> dict[str, dict]:
    """
    Planned budgets using v1/costCodes which includes supplyDollars.

    v2/costCodes only returns: laborDollars, equipmentDollars, materialDollars, subcontractDollars
    v1/costCodes also returns: supplyDollars  ← this is what the portal includes in "Budget"

    total_budget = labor + equipment + material + subcontract + supply
    """
    print(f"\n[Call 4] GET /v1/costCodes per job  (planned budgets including supplyDollars)")
    budget_map: dict[str, dict] = {}

    V1_CC_URL = "https://api.hcssapps.com/heavyjob/api/v1/costCodes"

    for jid in job_ids:
        rows = fetch_pages(client, "GET", V1_CC_URL, params={"jobId": jid, "limit": 500})
        for cc in rows:
            ccid = cc.get("id")
            if ccid:
                l      = float(cc.get("laborDollars")       or 0)
                e      = float(cc.get("equipmentDollars")   or 0)
                m      = float(cc.get("materialDollars")    or 0)
                s      = float(cc.get("subcontractDollars") or 0)
                supply = float(cc.get("supplyDollars")      or 0)  # ← KEY FIX
                total  = l + e + m + s + supply
                budget_map[ccid] = {
                    "total":    total,
                    "labor":    l,
                    "equip":    e,
                    "mat":      m,
                    "sub":      s,
                    "supply":   supply,
                    "cc_code":  cc.get("code") or "",
                    "cc_desc":  cc.get("description") or "",
                    "job_id":   cc.get("jobId") or jid,
                    "job_code": cc.get("jobCode") or "",
                }

    print(f"  {len(budget_map)} cost codes with budget data")
    return budget_map


def build_results(agg: dict[str, dict], budget_map: dict[str, dict], top: int) -> list[dict]:
    # Enrich agg with job/cc info from budget_map (v1/jobCosts is flat, no job/cc names)
    for ccid, v in agg.items():
        if ccid in budget_map and not v["cc_code"]:
            b = budget_map[ccid]
            v["cc_code"]  = b.get("cc_code", "")
            v["cc_desc"]  = b.get("cc_desc", "")
            v["job_id"]   = b.get("job_id",  "")
            v["job_code"] = b.get("job_code", "")

    results = []
    for ccid, v in agg.items():
        labor      = round(v["labor_cost"], 2)
        equip      = round(v["equip_cost"],  2)
        mat        = round(v["mat_cost"],    2)
        sub        = round(v["sub_cost"],    2)
        total_act  = round(labor + equip + mat + sub, 2)

        if total_act == 0:
            continue  # skip zero-cost rows

        b          = budget_map.get(ccid, {})
        total_bud  = round(b.get("total", 0.0), 2)
        util       = round((total_act / total_bud) * 100, 2) if total_bud > 0 else None
        variance   = round(total_bud - total_act, 2)

        if total_bud <= 0:
            status = "OVER_RISK" if total_act > 0 else "ON_TRACK"
        elif util is not None and util >= 75:
            status = "OVER_RISK"
        else:
            status = "ON_TRACK"

        results.append({
            "job_id":          v.get("job_id", ""),
            "job_code":        v.get("job_code", ""),
            "cost_code_id":    ccid,
            "cost_code":       v.get("cc_code", "") or b.get("cc_code", ""),
            "description":     v.get("cc_desc", "") or b.get("cc_desc", ""),
            "actual_cost":     total_act,
            "labor_cost":      labor,
            "equipment_cost":  equip,
            "material_cost":   mat,
            "subcontract_cost": sub,
            "labor_hours":     round(v["labor_hours"], 2),
            "expected_budget": total_bud,
            "labor_budget":    round(b.get("labor",  0.0), 2),
            "equipment_budget":round(b.get("equip",  0.0), 2),
            "material_budget": round(b.get("mat",    0.0), 2),
            "subcontract_budget": round(b.get("sub", 0.0), 2),
            "supply_budget":   round(b.get("supply", 0.0), 2),
            "utilization_pct": util,
            "variance":        variance,
            "status":          status,
        })

    # Sort by actual_cost descending, take top N
    results.sort(key=lambda r: r["actual_cost"], reverse=True)
    return results[:top]


def print_table(results: list[dict], start: str, end: str) -> None:
    print(f"\n{'='*120}")
    print(f"  HYBRID BUDGET RESULTS  —  {start} → {end}")
    print(f"  actual_cost = labor + equipment + material + subcontract (ALL TYPES, DATE-FILTERED)")
    print(f"  Compare to HCSS portal with SAME date filter applied")
    print(f"{'='*120}")
    print(f"  {'Job':<8}  {'Code':<16}  {'Description':<30}  "
          f"{'Budget':>12}  {'Actual':>12}  {'Util%':>7}  {'Status':<10}  "
          f"{'Labor':>10}  {'Mat':>10}  {'Sub':>10}")
    print(f"  {'-'*8}  {'-'*16}  {'-'*30}  {'-'*12}  {'-'*12}  {'-'*7}  {'-'*10}  {'-'*10}  {'-'*10}  {'-'*10}")

    for r in results:
        util_str = f"{r['utilization_pct']}%" if r["utilization_pct"] is not None else "  N/A"
        desc = r["description"][:30]
        print(
            f"  {r['job_code']:<8}  {r['cost_code']:<16}  {desc:<30}  "
            f"${r['expected_budget']:>11,.2f}  ${r['actual_cost']:>11,.2f}  "
            f"{util_str:>7}  {r['status']:<10}  "
            f"${r['labor_cost']:>9,.2f}  ${r['material_cost']:>9,.2f}  "
            f"${r['subcontract_cost']:>9,.2f}"
        )

    print(f"  {'─'*120}")
    total_act = sum(r["actual_cost"]     for r in results)
    total_bud = sum(r["expected_budget"] for r in results)
    print(f"  TOTALS  {total_bud:>66,.2f}  ${total_act:>11,.2f}")

    print(f"\n  HOW TO VERIFY IN HCSS PORTAL:")
    print(f"    1. Set date filter to {start} → {end}")
    print(f"    2. Compare 'Actual All Cost' column = our actual_cost")
    print(f"    3. Compare 'Budget' column = our expected_budget")
    print(f"    4. Compare 'Cost %' column = our utilization_pct")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", default="2026-06-01", help="Start date YYYY-MM-DD")
    parser.add_argument("--end",   default="2026-06-07", help="End date YYYY-MM-DD")
    parser.add_argument("--top",   type=int, default=20, help="Top N cost codes by actual cost (default 20)")
    args = parser.parse_args()

    print("=" * 70)
    print("  HYBRID BUDGET TEST")
    print("=" * 70)
    print(f"  Date range : {args.start} → {args.end}")
    print(f"  Top N      : {args.top} cost codes by actual cost")
    print(f"  Zero-cost codes are excluded from output")

    client = HCSSClient()

    # Step 1: find active jobs
    job_ids = get_active_job_ids(client, args.start, args.end)
    if not job_ids:
        print("\n  No active jobs in this window. Try a different date range.")
        sys.exit(0)

    # Calls 1-3: fetch all cost types, merge into agg
    agg: dict[str, dict] = {}
    agg = fetch_labor_equipment(client, job_ids, args.start, args.end)
    fetch_material_costs(client, job_ids, args.start, args.end, agg)
    fetch_subcontract_costs(client, job_ids, args.start, args.end, agg)

    # Call 4: planned budgets
    budget_map = fetch_budgets(client, job_ids)

    # Build results
    print(f"\n[Building] Merging all cost types and joining budgets...")
    results = build_results(agg, budget_map, args.top)
    print(f"  {len(results)} cost codes with actual cost > 0 (showing top {args.top})")

    # Print table
    print_table(results, args.start, args.end)

    # Save JSON
    out_path = Path(__file__).parent / "test_hybrid_budget_output.json"
    out_path.write_text(json.dumps({
        "meta": {
            "start_date":          args.start,
            "end_date":            args.end,
            "active_jobs":         len(job_ids),
            "total_cost_codes_with_cost": len(results),
            "note": (
                "actual_cost = labor + equipment + material + subcontract costs "
                f"posted in {args.start} → {args.end}. "
                "Compare to HCSS portal with same date filter. "
                "expected_budget is always the full project budget (not date-filtered)."
            ),
        },
        "results": results,
    }, indent=2))

    print(f"\n  Full output → {out_path.name}")
    print()


if __name__ == "__main__":
    main()
