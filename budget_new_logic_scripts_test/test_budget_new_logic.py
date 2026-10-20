"""
test_budget_new_logic.py
─────────────────────────────────────────────────────────────────────────────
STANDALONE test for budget data accuracy.

TWO MODES — selectable via --mode flag:

  --mode period  (DEFAULT)
    Uses /v2/jobCosts/advancedRequest with your date range.
    actual_cost = costs incurred IN that specific window only.
    This matches the portal when you filter to the same date range.
    The "Actual All Cost" column you see in the portal for a filtered period.

  --mode cumulative
    Uses /v1/jobCostsToDate/search (no date filter).
    actual_cost = LIFETIME total from job inception to now.
    Matches the portal's "Cost to Date" column with NO date filter applied.

DOES NOT touch any existing files. Zero side effects.

USAGE:
  python test_budget_new_logic.py                                    # last 7 days, period mode
  python test_budget_new_logic.py --start 2026-05-01 --end 2026-05-31
  python test_budget_new_logic.py --start 2026-05-01 --end 2026-05-31 --mode cumulative
  python test_budget_new_logic.py --start 2026-05-01 --end 2026-05-31 --job 25006
  python test_budget_new_logic.py --start 2026-05-01 --end 2026-05-31 --top 20

OUTPUT:
  - Console table sorted by actual_cost descending
  - Full JSON saved to: test_budget_new_logic_output.json
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any

# ── Bootstrap path so we can reuse HCSSClient auth ───────────────────────────
sys.path.insert(0, str(Path(__file__).resolve().parent))
from dotenv import load_dotenv
load_dotenv()

from payroll_verification.hcss_client import HCSSClient

# ── Endpoints ─────────────────────────────────────────────────────────────────
TIMECARDS_URL      = "https://api.hcssapps.com/heavyjob/api/v1/timeCardInfo"
JOB_COSTS_URL      = "https://api.hcssapps.com/heavyjob/api/v2/jobCosts/advancedRequest"
COSTS_TO_DATE_URL  = "https://api.hcssapps.com/heavyjob/api/v1/jobCostsToDate/search"
COST_CODES_URL     = "https://api.hcssapps.com/heavyjob/api/v2/costCodes/advancedRequest"


# ─────────────────────────────────────────────────────────────────────────────
# CALL 1 — discover active job IDs from timecards in the window
# ─────────────────────────────────────────────────────────────────────────────
def fetch_active_job_ids(client: HCSSClient, start: str, end: str) -> dict[str, str]:
    """
    Returns { job_id: job_code } for every job with timecard activity
    in the given date window. Used to scope the expensive Calls 2+3.
    """
    print(f"\n[Call 1] GET /timeCardInfo  {start} → {end}")
    params: dict[str, Any] = {"startDate": start, "endDate": end}
    all_tcs: list[dict] = []
    cursor = None

    while True:
        if cursor:
            params["cursor"] = cursor
        raw = client._request("GET", TIMECARDS_URL, params=params)
        results, next_cursor = client._extract_results_and_cursor(raw)
        all_tcs.extend(results)
        if not next_cursor:
            break
        cursor = next_cursor

    job_map: dict[str, str] = {}
    for tc in all_tcs:
        jid   = tc.get("jobId") or tc.get("job_id") or ""
        jcode = tc.get("jobCode") or tc.get("jobDescription") or "?"
        if jid:
            job_map[jid] = jcode

    print(f"  → {len(all_tcs)} timecards  |  {len(job_map)} unique active jobs")
    return job_map


# ─────────────────────────────────────────────────────────────────────────────
# CALL 2a — period actual costs (date-filtered, matches portal filtered view)
# ─────────────────────────────────────────────────────────────────────────────
def fetch_costs_period(client: HCSSClient, job_ids: list[str], start: str, end: str) -> list[dict]:
    """
    POST /v2/jobCosts/advancedRequest
    Date-filtered: returns costs ONLY incurred in the given window.
    Matches the HCSS portal when you apply the same date filter.
    Returns one row per (job, costCode, foreman) — must aggregate by (job, costCode).
    """
    print(f"\n[Call 2] POST /v2/jobCosts/advancedRequest  ({len(job_ids)} jobs, {start}→{end})")
    payload: dict[str, Any] = {
        "jobIds":    job_ids,
        "startDate": start,
        "endDate":   end,
        "limit":     500,
    }
    all_rows: list[dict] = []
    cursor = None
    page = 0

    while True:
        if cursor:
            payload["cursor"] = cursor
        raw = client._request("POST", JOB_COSTS_URL, json=payload)
        results, next_cursor = client._extract_results_and_cursor(raw)
        all_rows.extend(results)
        page += 1
        print(f"  page {page}: {len(results)} rows  (running total: {len(all_rows)})")
        if not next_cursor:
            break
        cursor = next_cursor

    print(f"  → {len(all_rows)} total cost rows (period {start}→{end})")
    return all_rows


# ─────────────────────────────────────────────────────────────────────────────
# CALL 2b — cumulative actual costs (no date filter, lifetime totals)
# ─────────────────────────────────────────────────────────────────────────────
def fetch_costs_to_date(client: HCSSClient, job_ids: list[str]) -> list[dict]:
    """
    POST /v1/jobCostsToDate/search
    NO date filter: returns CUMULATIVE costs from job inception to now.
    Matches the HCSS portal "Cost to Date" with no date filter applied.
    """
    print(f"\n[Call 2] POST /v1/jobCostsToDate/search  ({len(job_ids)} jobs, cumulative)")
    payload: dict[str, Any] = {"jobIds": job_ids, "limit": 500}
    all_rows: list[dict] = []
    cursor = None
    page = 0

    while True:
        if cursor:
            payload["cursor"] = cursor
        raw = client._request("POST", COSTS_TO_DATE_URL, json=payload)
        results, next_cursor = client._extract_results_and_cursor(raw)
        all_rows.extend(results)
        page += 1
        print(f"  page {page}: {len(results)} rows  (running total: {len(all_rows)})")
        if not next_cursor:
            break
        cursor = next_cursor

    print(f"  → {len(all_rows)} total cost rows (cumulative to date)")
    return all_rows


# ─────────────────────────────────────────────────────────────────────────────
# CALL 3 — planned budget per cost code
# ─────────────────────────────────────────────────────────────────────────────
def fetch_cost_code_budgets(client: HCSSClient, job_ids: list[str]) -> dict[str, dict]:
    """
    POST /v2/costCodes/advancedRequest
    Returns { costCodeId → budget dict } with laborDollars, equipmentDollars etc.
    """
    print(f"\n[Call 3] POST /costCodes/advancedRequest  ({len(job_ids)} jobs)")
    payload: dict[str, Any] = {"jobIds": job_ids, "limit": 500}
    budget_map: dict[str, dict] = {}
    cursor = None
    page = 0

    while True:
        if cursor:
            payload["cursor"] = cursor
        raw = client._request("POST", COST_CODES_URL, json=payload)
        results, next_cursor = client._extract_results_and_cursor(raw)
        page += 1
        for cc in results:
            ccid = cc.get("id")
            if ccid:
                budget_map[ccid] = cc
        print(f"  page {page}: {len(results)} rows  (running total: {len(budget_map)})")
        if not next_cursor:
            break
        cursor = next_cursor

    print(f"  → {len(budget_map)} total cost codes with budget data")
    return budget_map


# ─────────────────────────────────────────────────────────────────────────────
# AGGREGATE + JOIN
# ─────────────────────────────────────────────────────────────────────────────
def build_budget_rows(
    cost_rows: list[dict],
    budget_map: dict[str, dict],
) -> list[dict]:
    """
    Aggregate cost rows by (job_id, cost_code_id), join budget, compute status.
    Returns list sorted by actual_cost descending.
    """
    agg: dict[tuple, dict] = {}

    for row in cost_rows:
        job_info     = row.get("job") or {}
        cc_info      = row.get("costCode") or {}
        foreman_info = row.get("foreman") or {}

        jid   = job_info.get("jobId", "")
        ccid  = cc_info.get("costCodeId", "")
        if not jid or not ccid:
            continue

        key = (jid, ccid)

        # v1 uses "totalLaborCost", v2 uses "laborCost" — handle both
        labor  = float(row.get("totalLaborCost")       or row.get("laborCost")       or 0)
        equip  = float(row.get("totalEquipmentCost")   or row.get("equipmentCost")   or 0)
        mat    = float(row.get("totalMaterialCost")    or row.get("materialCost")    or 0)
        sub    = float(row.get("totalSubcontractCost") or row.get("subcontractCost") or 0)
        truck  = float(row.get("totalTruckingCost")    or row.get("truckingCost")    or 0)
        total  = labor + equip + mat + sub + truck
        hours  = float(row.get("totalLaborHours") or row.get("laborHours") or 0)
        qty    = float(row.get("totalQuantity")   or row.get("quantity")   or 0)

        fname = (
            f"{foreman_info.get('employeeFirstName','')} "
            f"{foreman_info.get('employeeLastName','')}".strip()
            or foreman_info.get("employeeCode", "")
        )

        if key in agg:
            agg[key]["actual_cost"]      += total
            agg[key]["labor_cost"]       += labor
            agg[key]["equipment_cost"]   += equip
            agg[key]["material_cost"]    += mat
            agg[key]["subcontract_cost"] += sub
            agg[key]["trucking_cost"]    += truck
            agg[key]["labor_hours"]      += hours
            agg[key]["quantity"]         += qty
            if fname and fname not in agg[key]["_foremen_seen"]:
                agg[key]["foremen"].append(fname)
                agg[key]["_foremen_seen"].add(fname)
        else:
            agg[key] = {
                "job_id":          jid,
                "job_code":        job_info.get("jobCode", ""),
                "job_description": job_info.get("jobDescription", ""),
                "cost_code_id":    ccid,
                "cost_code":       cc_info.get("costCodeCode", ""),
                "description":     cc_info.get("costCodeDescription", ""),
                "actual_cost":     total,
                "labor_cost":      labor,
                "equipment_cost":  equip,
                "material_cost":   mat,
                "subcontract_cost": sub,
                "trucking_cost":   truck,
                "labor_hours":     hours,
                "quantity":        qty,
                "foremen":         [fname] if fname else [],
                "_foremen_seen":   {fname} if fname else set(),
                # Budget — filled below
                "expected_budget":    0.0,
                "labor_budget":       0.0,
                "equipment_budget":   0.0,
                "material_budget":    0.0,
                "subcontract_budget": 0.0,
                "utilization_pct":    None,
                "variance":           0.0,
                "status":             "ON_TRACK",
            }

    # Join budget
    results = []
    for item in agg.values():
        item.pop("_foremen_seen", None)
        ccid = item["cost_code_id"]
        b    = budget_map.get(ccid, {})

        l_bud  = float(b.get("laborDollars")       or 0)
        e_bud  = float(b.get("equipmentDollars")   or 0)
        m_bud  = float(b.get("materialDollars")    or 0)
        s_bud  = float(b.get("subcontractDollars") or 0)
        t_bud  = l_bud + e_bud + m_bud + s_bud

        actual = item["actual_cost"]
        util   = round((actual / t_bud) * 100, 1) if t_bud > 0 else None
        var    = round(t_bud - actual, 2)

        if t_bud <= 0:
            status = "OVER_RISK" if actual > 0 else "ON_TRACK"
        elif util is not None and util >= 75:
            status = "OVER_RISK"
        else:
            status = "ON_TRACK"

        item.update({
            "expected_budget":    round(t_bud, 2),
            "labor_budget":       round(l_bud, 2),
            "equipment_budget":   round(e_bud, 2),
            "material_budget":    round(m_bud, 2),
            "subcontract_budget": round(s_bud, 2),
            "utilization_pct":    util,
            "variance":           var,
            "status":             status,
        })
        results.append(item)

    # Sort by actual_cost descending — highest spend first
    results.sort(key=lambda x: x["actual_cost"], reverse=True)
    return results


# ─────────────────────────────────────────────────────────────────────────────
# PRINT TABLE
# ─────────────────────────────────────────────────────────────────────────────
def print_table(rows: list[dict], top: int) -> None:
    shown = rows[:top]
    total_actual  = sum(r["actual_cost"]    for r in rows)
    total_budget  = sum(r["expected_budget"] for r in rows)
    nonzero_count = sum(1 for r in rows if r["actual_cost"] > 0)
    zero_count    = sum(1 for r in rows if r["actual_cost"] == 0)
    over_risk     = sum(1 for r in rows if r["status"] == "OVER_RISK")

    print("\n" + "═" * 130)
    print("  NEW LOGIC RESULTS  —  jobCostsToDate (CUMULATIVE, all cost types)")
    print("═" * 130)
    print(f"  Total cost codes : {len(rows)}")
    print(f"  With actual cost : {nonzero_count}  ← was mostly 0 with old logic")
    print(f"  Zero actual cost : {zero_count}     ← cost codes with no spend at all")
    print(f"  OVER_RISK        : {over_risk}")
    print(f"  Total actual     : ${total_actual:>15,.2f}")
    print(f"  Total budget     : ${total_budget:>15,.2f}")
    if total_budget > 0:
        overall_util = round(total_actual / total_budget * 100, 1)
        print(f"  Overall util     : {overall_util}%")
    print(f"\n  Showing top {top} rows by actual cost (full data in JSON):")
    print("─" * 130)
    print(f"  {'Job':<10}  {'Cost Code':<16}  {'Description':<35}  "
          f"{'Budget':>12}  {'Actual':>12}  {'Util%':>6}  {'Status':<10}  "
          f"{'Labor':>10}  {'Mat':>10}  {'Sub':>10}")
    print("─" * 130)

    for r in shown:
        util_str = f"{r['utilization_pct']:.1f}%" if r["utilization_pct"] is not None else "  N/A"
        desc     = r["description"][:35]
        print(
            f"  {r['job_code']:<10}  {r['cost_code']:<16}  {desc:<35}  "
            f"${r['expected_budget']:>11,.2f}  ${r['actual_cost']:>11,.2f}  "
            f"{util_str:>6}  {r['status']:<10}  "
            f"${r['labor_cost']:>9,.2f}  ${r['material_cost']:>9,.2f}  "
            f"${r['subcontract_cost']:>9,.2f}"
        )

    if len(rows) > top:
        print(f"\n  ... and {len(rows) - top} more rows in the JSON file.")
    print("─" * 130)


# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser(
        description="Test budget data accuracy — standalone, no DB writes."
    )
    parser.add_argument(
        "--start",
        default=(date.today() - timedelta(days=7)).isoformat(),
        help="Start date YYYY-MM-DD. Default: 7 days ago.",
    )
    parser.add_argument(
        "--end",
        default=date.today().isoformat(),
        help="End date YYYY-MM-DD. Default: today.",
    )
    parser.add_argument(
        "--mode",
        default="period",
        choices=["period", "cumulative"],
        help=(
            "period     = costs incurred IN the date window only. "
            "Matches the portal when you filter to the same dates. (DEFAULT)\n"
            "cumulative = lifetime totals from job inception to now. "
            "Matches portal with no date filter."
        ),
    )
    parser.add_argument(
        "--job",
        default=None,
        help="Optional job CODE to filter output (e.g. 25006). Does not affect API calls.",
    )
    parser.add_argument(
        "--top",
        type=int,
        default=50,
        help="How many rows to show in the console table. Default: 50.",
    )
    args = parser.parse_args()

    print("=" * 70)
    print("  BUDGET ACCURACY TEST")
    print("=" * 70)
    print(f"  Date window  : {args.start} → {args.end}")
    print(f"  Mode         : {args.mode.upper()}")
    if args.mode == "period":
        print(f"  Endpoint     : /v2/jobCosts/advancedRequest (date-filtered)")
        print(f"  Matches      : HCSS portal filtered to same date range")
        print(f"  actual_cost  : costs posted IN this window only")
    else:
        print(f"  Endpoint     : /v1/jobCostsToDate/search (no date filter)")
        print(f"  Matches      : HCSS portal with no date filter (Cost to Date)")
        print(f"  actual_cost  : lifetime cumulative total")
    print(f"  Job filter   : {args.job or 'all'}")
    print(f"  Console rows : top {args.top} by actual cost")
    print("=" * 70)

    client = HCSSClient()

    # ── Call 1: discover active jobs via timecards ─────────────────────
    try:
        job_map = fetch_active_job_ids(client, args.start, args.end)
    except Exception as exc:
        print(f"\n[ERROR] Call 1 failed: {exc}")
        sys.exit(1)

    if not job_map:
        print("\n  No timecard activity found in that window — nothing to fetch.")
        print("  Try widening the date range.")
        sys.exit(0)

    job_ids = list(job_map.keys())

    # ── Call 2: actual costs (period or cumulative) ────────────────────
    try:
        if args.mode == "period":
            cost_rows = fetch_costs_period(client, job_ids, args.start, args.end)
        else:
            cost_rows = fetch_costs_to_date(client, job_ids)
    except Exception as exc:
        print(f"\n[ERROR] Call 2 failed: {exc}")
        sys.exit(1)

    if not cost_rows:
        print(f"\n  0 cost rows returned in {args.mode} mode for this window.")
        print("  Try a wider date range or --mode cumulative.")
        sys.exit(0)

    # ── Call 3: planned budget ─────────────────────────────────────────
    try:
        budget_map = fetch_cost_code_budgets(client, job_ids)
    except Exception as exc:
        print(f"\n[ERROR] Call 3 failed: {exc}")
        sys.exit(1)

    # ── Build, filter, display ─────────────────────────────────────────
    print("\n[Building] Aggregating and joining budget...")
    rows = build_budget_rows(cost_rows, budget_map)

    if args.job:
        rows_filtered = [r for r in rows if args.job.lower() in r["job_code"].lower()]
        print(f"  Job filter '{args.job}': {len(rows)} → {len(rows_filtered)} rows")
        rows = rows_filtered

    print_table(rows, args.top)

    # ── Save full JSON ─────────────────────────────────────────────────
    out_path = Path(__file__).parent / "test_budget_new_logic_output.json"
    out_path.write_text(
        json.dumps(
            {
                "meta": {
                    "mode":            args.mode,
                    "window_start":    args.start,
                    "window_end":      args.end,
                    "active_jobs":     len(job_map),
                    "total_cost_rows": len(cost_rows),
                    "total_cc_budget": len(budget_map),
                    "result_rows":     len(rows),
                    "note": (
                        "period mode: actual_cost = costs posted in window only. "
                        "cumulative mode: actual_cost = lifetime total to date."
                    ),
                },
                "jobs_found": job_map,
                "results": rows,
            },
            indent=2,
            default=str,
        )
    )
    print(f"\n  Full JSON saved → {out_path.name}")
    print("\n  How to verify in HCSS portal:")
    if args.mode == "period":
        print(f"    Set portal date filter to {args.start} → {args.end}")
        print("    'Actual All Cost' column = our actual_cost")
    else:
        print("    Remove date filter in portal (show all time)")
        print("    'Cost to Date' column = our actual_cost")
    print("    'Budget' column  = our expected_budget")
    print("    'Cost %' column  = our utilization_pct")
    print()


if __name__ == "__main__":
    main()
