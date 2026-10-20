"""
test_v1_jobcosts_startdate.py
─────────────────────────────────────────────────────────────────────────────
Tests the v1/jobCosts/advancedRequest endpoint with startDate only (no endDate).

According to HCSS docs, v1 jobCosts with only startDate returns ALL costs
from that date to now — like the portal date filter.

This test:
  1. Calls v1/jobCosts with startDate=2026-05-25 (no endDate) for job 26004
  2. Calls v1/jobCostsToDate (no date) for the same job — cumulative baseline
  3. Compares results for specific cost codes against HCSS portal

Run:
  python test_v1_jobcosts_startdate.py
  python test_v1_jobcosts_startdate.py --start 2026-01-01
  python test_v1_jobcosts_startdate.py --start 2026-05-01 --job 26004 --top 20
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from dotenv import load_dotenv
load_dotenv()

from payroll_verification.hcss_client import HCSSClient

# Endpoints
V1_JOBCOSTS_URL    = "https://api.hcssapps.com/heavyjob/api/v1/jobCosts/advancedRequest"
V1_TODATE_URL      = "https://api.hcssapps.com/heavyjob/api/v1/jobCostsToDate/search"
JOBS_URL           = "https://api.hcssapps.com/heavyjob/api/v1/jobs"
COST_CODES_URL     = "https://api.hcssapps.com/heavyjob/api/v2/costCodes/advancedRequest"


def fetch_pages(client: HCSSClient, method: str, url: str,
                payload: dict | None = None, params: dict | None = None) -> list[dict]:
    rows, cursor = [], None
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
        if not next_cursor:
            break
        cursor = next_cursor
    return rows


def aggregate_costs(rows: list[dict]) -> dict[str, dict]:
    """Aggregate rows by cost_code_id, summing all cost fields."""
    agg: dict[str, dict] = {}
    for row in rows:
        cc_info = row.get("costCode") or {}
        ccid = cc_info.get("costCodeId") or cc_info.get("id") or ""
        if not ccid:
            continue

        # v1/jobCosts uses non-prefixed names: laborCost, equipmentCost etc.
        # v1/jobCostsToDate uses total-prefixed: totalLaborCost etc.
        labor = float(row.get("laborCost")       or row.get("totalLaborCost")       or 0)
        equip = float(row.get("equipmentCost")   or row.get("totalEquipmentCost")   or 0)
        mat   = float(row.get("materialCost")    or row.get("totalMaterialCost")    or 0)
        sub   = float(row.get("subcontractCost") or row.get("totalSubcontractCost") or 0)
        truck = float(row.get("truckingCost")    or row.get("totalTruckingCost")    or 0)
        hours = float(row.get("laborHours")      or row.get("totalLaborHours")      or 0)
        qty   = float(row.get("quantity")        or row.get("totalQuantity")        or 0)
        total = labor + equip + mat + sub + truck

        if ccid in agg:
            agg[ccid]["labor"]  += labor
            agg[ccid]["equip"]  += equip
            agg[ccid]["mat"]    += mat
            agg[ccid]["sub"]    += sub
            agg[ccid]["truck"]  += truck
            agg[ccid]["hours"]  += hours
            agg[ccid]["qty"]    += qty
            agg[ccid]["total"]  += total
        else:
            agg[ccid] = {
                "cc_code": (
                    cc_info.get("costCodeCode")
                    or cc_info.get("code")
                    or ccid[:8]
                ),
                "description": (
                    cc_info.get("costCodeDescription")
                    or cc_info.get("description")
                    or ""
                ),
                "job_id": (row.get("job") or {}).get("jobId", ""),
                "labor": labor, "equip": equip, "mat": mat,
                "sub": sub, "truck": truck, "hours": hours,
                "qty": qty, "total": total,
            }
    return agg


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", default="2026-05-25",
                        help="startDate for v1/jobCosts (default: 2026-05-25)")
    parser.add_argument("--job",   default="26004",
                        help="Job CODE to filter results (default: 26004)")
    parser.add_argument("--top",   type=int, default=30,
                        help="Max rows to print (default: 30)")
    args = parser.parse_args()

    client = HCSSClient()

    # ── Step 1: Get active jobs and find the target job ───────────────────────
    print(f"\n[Step 1] Fetching active jobs to find job code '{args.job}'...")
    raw_jobs = client._request("GET", JOBS_URL)
    all_jobs = raw_jobs if isinstance(raw_jobs, list) else raw_jobs.get("results", []) or []
    active_jobs = [
        j for j in all_jobs
        if not j.get("isDeleted", False)
        and (j.get("status") or "").lower() not in ("closed", "complete", "completed")
    ]
    target_jobs = [j for j in active_jobs if j.get("code", "") == args.job]
    if not target_jobs:
        # Partial match
        target_jobs = [j for j in active_jobs if args.job.lower() in (j.get("code") or "").lower()]

    if target_jobs:
        job_ids = [j["id"] for j in target_jobs]
        print(f"  Found {len(target_jobs)} job(s): {[j.get('code') for j in target_jobs]}")
    else:
        # Use all active jobs if no match
        job_ids = [j["id"] for j in active_jobs if j.get("id")]
        print(f"  Job '{args.job}' not found — using all {len(job_ids)} active jobs")

    # ── Step 2: Planned budgets from costCodes ────────────────────────────────
    print(f"\n[Step 2] Fetching planned budgets from costCodes...")
    cc_rows = fetch_pages(client, "POST", COST_CODES_URL,
                          payload={"jobIds": job_ids, "limit": 500})
    budget_map: dict[str, dict] = {}
    for cc in cc_rows:
        ccid = cc.get("id")
        if ccid:
            l = float(cc.get("laborDollars")       or 0)
            e = float(cc.get("equipmentDollars")   or 0)
            m = float(cc.get("materialDollars")    or 0)
            s = float(cc.get("subcontractDollars") or 0)
            budget_map[ccid] = {
                "code":        cc.get("code") or "",
                "description": cc.get("description") or "",
                "total_bud":   l + e + m + s,
                "labor_bud":   l, "equip_bud": e,
                "mat_bud":     m, "sub_bud":   s,
            }
    print(f"  {len(budget_map)} cost codes with budget data")

    # ── Step 3a: v1/jobCosts with startDate only (NEW approach) ──────────────
    print(f"\n[Step 3a] POST /v1/jobCosts/advancedRequest  startDate={args.start} (no endDate)")
    print(f"  This should return ALL costs from {args.start} to today (like portal date filter)")
    v1_rows = fetch_pages(client, "POST", V1_JOBCOSTS_URL, payload={
        "jobIds":    job_ids,
        "startDate": args.start,
        # NO endDate — should mean "from startDate to now"
        "limit":     500,
    })
    print(f"  Raw rows returned: {len(v1_rows)}")
    if v1_rows:
        sample = v1_rows[0]
        print(f"  Field names in response: {list(sample.keys())}")
        # Check what cost fields are present
        cc_sample = sample.get("costCode") or {}
        print(f"  costCode keys: {list(cc_sample.keys())}")

    v1_agg = aggregate_costs(v1_rows)
    v1_nonzero = sum(1 for v in v1_agg.values() if v["total"] > 0)
    print(f"  Aggregated: {len(v1_agg)} cost codes  ({v1_nonzero} with cost > 0)")

    # ── Step 3b: v1/jobCostsToDate (no date) — cumulative baseline ───────────
    print(f"\n[Step 3b] POST /v1/jobCostsToDate/search  (no date = full cumulative)")
    v1td_rows = fetch_pages(client, "POST", V1_TODATE_URL, payload={
        "jobIds": job_ids,
        "limit":  500,
    })
    print(f"  Raw rows returned: {len(v1td_rows)}")
    v1td_agg = aggregate_costs(v1td_rows)
    v1td_nonzero = sum(1 for v in v1td_agg.values() if v["total"] > 0)
    print(f"  Aggregated: {len(v1td_agg)} cost codes  ({v1td_nonzero} with cost > 0)")

    # ── Step 4: Comparison table ──────────────────────────────────────────────
    print(f"\n{'='*110}")
    print(f"  COMPARISON: v1/jobCosts (startDate={args.start}) vs v1/jobCostsToDate (cumulative)")
    print(f"  Budget from costCodes endpoint  |  Portal: remove date filter = cumulative, apply date = from startDate")
    print(f"{'='*110}")
    print(f"  {'Code':<16} {'Budget':>12} {'v1_startDate':>14} {'v1_toDate':>12} {'Match?':>8}  Description")
    print(f"  {'-'*16} {'-'*12} {'-'*14} {'-'*12} {'-'*8}  {'-'*30}")

    # Show cost codes that have data in either source, sorted by v1_startDate desc
    all_ccids = set(v1_agg.keys()) | set(v1td_agg.keys())
    rows_to_show = []
    for ccid in all_ccids:
        v1_total  = v1_agg.get(ccid,   {}).get("total", 0.0)
        v1td_total = v1td_agg.get(ccid, {}).get("total", 0.0)
        if v1_total == 0 and v1td_total == 0:
            continue  # skip zero on both
        b = budget_map.get(ccid, {})
        cc_code = (
            (v1_agg.get(ccid) or v1td_agg.get(ccid) or {}).get("cc_code")
            or b.get("code", ccid[:8])
        )
        desc = (
            (v1_agg.get(ccid) or v1td_agg.get(ccid) or {}).get("description")
            or b.get("description", "")
        )[:30]
        budget = b.get("total_bud", 0.0)
        # Match = are v1_startDate and v1_toDate the same? (they should differ if start not project start)
        match = "SAME" if abs(v1_total - v1td_total) < 0.01 else "DIFF"
        rows_to_show.append((cc_code, budget, v1_total, v1td_total, match, desc))

    rows_to_show.sort(key=lambda x: x[1], reverse=True)
    shown = rows_to_show[:args.top]

    same_count = sum(1 for r in rows_to_show if r[4] == "SAME")
    diff_count = sum(1 for r in rows_to_show if r[4] == "DIFF")

    for code, budget, v1t, v1td, match, desc in shown:
        m_str = "✅" if match == "SAME" else "📊"
        print(f"  {code:<16} ${budget:>11,.2f} ${v1t:>13,.2f} ${v1td:>11,.2f} {m_str:>8}  {desc}")

    if len(rows_to_show) > args.top:
        print(f"  ... {len(rows_to_show) - args.top} more rows in JSON output")

    print(f"\n{'='*110}")
    print(f"  Total with any cost : {len(rows_to_show)}")
    print(f"  SAME (no activity before {args.start}): {same_count}")
    print(f"  DIFF (has activity before {args.start}): {diff_count}")
    print(f"\n  KEY QUESTION: Is v1_startDate showing LESS than v1_toDate for cost codes")
    print(f"  that had activity before {args.start}?")
    print(f"  → If YES: the startDate filter is WORKING like the portal date filter ✅")
    print(f"  → If NO:  the endpoint is ignoring startDate and returning all-time totals")

    # ── Save output ───────────────────────────────────────────────────────────
    out = Path(__file__).parent / "test_v1_jobcosts_startdate_output.json"
    out.write_text(json.dumps({
        "test_config": {
            "start_date":  args.start,
            "job_filter":  args.job,
            "job_ids":     job_ids,
            "note": (
                f"v1_startDate = costs from {args.start} to today (v1/jobCosts with startDate only). "
                f"v1_toDate = all-time cumulative (v1/jobCostsToDate). "
                f"If they differ, the startDate filter is working."
            ),
        },
        "summary": {
            "total_cost_codes_with_data": len(rows_to_show),
            "same_as_cumulative":         same_count,
            "different_from_cumulative":  diff_count,
            "startdate_endpoint_works":   diff_count > 0,
        },
        "v1_startdate_agg":  {ccid: v for ccid, v in v1_agg.items()  if v["total"] > 0},
        "v1_todate_agg":     {ccid: v for ccid, v in v1td_agg.items() if v["total"] > 0},
    }, indent=2, default=str))

    print(f"\n  Full output → test_v1_jobcosts_startdate_output.json")
    print()


if __name__ == "__main__":
    main()
