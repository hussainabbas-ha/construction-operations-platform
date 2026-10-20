"""
verify_cost_code.py
─────────────────────────────────────────────────────────────────────────────
Comprehensive verification script for a specific cost code.

Tests:
  1. Raw v2/jobCosts rows   — individual daily entries
  2. Raw v1/jobCostsToDate  — cumulative totals
  3. Budget (costCodes)     — planned budget values
  4. Math verification      — manually sum v2 rows and compare to v1 totals
  5. Cross-check            — confirm v2 sum matches v1 cumulative
  6. Field name audit       — list ALL keys in each response so nothing is missed

Usage:
  python verify_cost_code.py                          # 61950960C, last 30 days
  python verify_cost_code.py --start 2026-05-01 --end 2026-05-31
  python verify_cost_code.py --code 61300200A --start 2026-05-01 --end 2026-05-31
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from dotenv import load_dotenv
load_dotenv()

from payroll_verification.hcss_client import HCSSClient

# ── Known cost codes for quick lookup ─────────────────────────────────────────
KNOWN_CODES = {
    "61950960C": "898ed2ca-faa2-4d18-b1bb-2b515e05a27e",
    "61300200A": "314d37e7-4b6c-46bb-8578-9ab7a7cf942f",
}
JOB_ID = "4e94268c-5630-4b7a-bba6-8000efbb65e1"   # 26004 — Hwy 6 & 24

V2_JOBCOSTS_URL   = "https://api.hcssapps.com/heavyjob/api/v2/jobCosts/advancedRequest"
V1_TODATE_URL     = "https://api.hcssapps.com/heavyjob/api/v1/jobCostsToDate/search"
COST_CODES_URL    = "https://api.hcssapps.com/heavyjob/api/v2/costCodes/advancedRequest"


def div(w=80): print("─" * w)
def hdiv(w=80): print("═" * w)


def fetch_all_pages(client, method, url, payload=None, params=None):
    rows = []
    cursor = None
    while True:
        if cursor:
            if payload is not None:
                payload["cursor"] = cursor
            else:
                params["cursor"] = cursor
        raw = client._request(method, url, json=payload, params=params)
        batch, next_cursor = client._extract_results_and_cursor(raw)
        rows.extend(batch)
        if not next_cursor:
            break
        cursor = next_cursor
    return rows


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--code",  default="61950960C", help="Cost code to verify")
    parser.add_argument("--start", default=(date.today() - timedelta(days=30)).isoformat())
    parser.add_argument("--end",   default=date.today().isoformat())
    args = parser.parse_args()

    cc_code = args.code
    cc_id   = KNOWN_CODES.get(cc_code)

    if not cc_id:
        print(f"  Cost code {cc_code} not in KNOWN_CODES dict.")
        print("  Add its UUID to the KNOWN_CODES dict at the top of this script.")
        sys.exit(1)

    client = HCSSClient()

    hdiv()
    print(f"  VERIFICATION: {cc_code}  ({cc_id[:8]}...)")
    print(f"  Job: 26004 — Hwy 6 & 24 Shared Path Improvements")
    print(f"  Window: {args.start} → {args.end}")
    hdiv()

    # ── TEST 1: v2/jobCosts — date-filtered individual rows ───────────────────
    print(f"\n[TEST 1] v2/jobCosts/advancedRequest  (date-filtered, {args.start}→{args.end})")
    v2_all = fetch_all_pages(client, "POST", V2_JOBCOSTS_URL, payload={
        "jobIds":      [JOB_ID],
        "costCodeIds": [cc_id],
        "startDate":   args.start,
        "endDate":     args.end,
        "limit":       500,
    })
    v2_rows = [r for r in v2_all if (r.get("costCode") or {}).get("costCodeId") == cc_id]
    print(f"  Rows returned for this cost code: {len(v2_rows)}")

    # Audit field names on first row
    if v2_rows:
        print(f"  Field names in v2 response: {list(v2_rows[0].keys())}")
        div()
        print(f"  {'Date':<14} {'Foreman':<25} {'Labor':>10} {'Equip':>10} {'Mat':>10} {'Sub':>10} {'Qty':>8}")
        div()
        v2_labor = v2_equip = v2_mat = v2_sub = v2_truck = v2_hours = v2_qty = 0.0
        for r in sorted(v2_rows, key=lambda x: x.get("date", "")):
            fmn   = r.get("foreman") or {}
            fname = f"{fmn.get('employeeFirstName','')} {fmn.get('employeeLastName','')}".strip()
            dt    = (r.get("date") or "")[:10]
            # v2 uses non-prefixed field names
            labor = float(r.get("laborCost")       or r.get("totalLaborCost")       or 0)
            equip = float(r.get("equipmentCost")   or r.get("totalEquipmentCost")   or 0)
            mat   = float(r.get("materialCost")    or r.get("totalMaterialCost")    or 0)
            sub   = float(r.get("subcontractCost") or r.get("totalSubcontractCost") or 0)
            truck = float(r.get("truckingCost")    or r.get("totalTruckingCost")    or 0)
            hours = float(r.get("laborHours")      or r.get("totalLaborHours")      or 0)
            qty   = float(r.get("quantity")        or r.get("totalQuantity")        or 0)
            print(f"  {dt:<14} {fname:<25} ${labor:>9,.2f} ${equip:>9,.2f} ${mat:>9,.2f} ${sub:>9,.2f} {qty:>7,.1f}")
            v2_labor += labor; v2_equip += equip; v2_mat += mat
            v2_sub += sub; v2_truck += truck; v2_hours += hours; v2_qty += qty
        div()
        v2_total = v2_labor + v2_equip + v2_mat + v2_sub + v2_truck
        print(f"  {'TOTAL v2 SUM':<14} {'':25} ${v2_labor:>9,.2f} ${v2_equip:>9,.2f} ${v2_mat:>9,.2f} ${v2_sub:>9,.2f} {v2_qty:>7,.1f}")
        print(f"  TOTAL ACTUAL COST (v2, period {args.start}→{args.end}): ${v2_total:,.2f}")
        print(f"  Total labor hours: {v2_hours:.1f}  Total quantity: {v2_qty:.1f}")
    else:
        v2_total = 0.0
        print("  ✗ No rows found — cost code had no activity in this period")

    # ── TEST 2: v1/jobCostsToDate — cumulative totals ─────────────────────────
    print(f"\n[TEST 2] v1/jobCostsToDate/search  (CUMULATIVE — no date filter)")
    v1_all = fetch_all_pages(client, "POST", V1_TODATE_URL, payload={
        "jobIds":      [JOB_ID],
        "costCodeIds": [cc_id],
        "limit":       500,
    })
    v1_rows = [r for r in v1_all if (r.get("costCode") or {}).get("costCodeId") == cc_id]
    print(f"  Rows returned: {len(v1_rows)}")

    if v1_rows:
        print(f"  Field names in v1 response: {list(v1_rows[0].keys())}")
        div()
        print(f"  {'Foreman':<30} {'Labor':>12} {'Equip':>12} {'Mat':>12} {'Sub':>12}")
        div()
        v1_labor = v1_equip = v1_mat = v1_sub = v1_truck = v1_hours = v1_qty = 0.0
        for r in v1_rows:
            fmn   = r.get("foreman") or {}
            fname = f"{fmn.get('employeeFirstName','')} {fmn.get('employeeLastName','')}".strip()
            # v1 uses total-prefixed field names
            labor = float(r.get("totalLaborCost")       or r.get("laborCost")       or 0)
            equip = float(r.get("totalEquipmentCost")   or r.get("equipmentCost")   or 0)
            mat   = float(r.get("totalMaterialCost")    or r.get("materialCost")    or 0)
            sub   = float(r.get("totalSubcontractCost") or r.get("subcontractCost") or 0)
            truck = float(r.get("totalTruckingCost")    or r.get("truckingCost")    or 0)
            hours = float(r.get("totalLaborHours")      or r.get("laborHours")      or 0)
            qty   = float(r.get("totalQuantity")        or r.get("quantity")        or 0)
            print(f"  {fname:<30} ${labor:>11,.2f} ${equip:>11,.2f} ${mat:>11,.2f} ${sub:>11,.2f}")
            v1_labor += labor; v1_equip += equip; v1_mat += mat
            v1_sub += sub; v1_truck += truck; v1_hours += hours; v1_qty += qty
        div()
        v1_total = v1_labor + v1_equip + v1_mat + v1_sub + v1_truck
        print(f"  TOTAL CUMULATIVE COST (v1, all time): ${v1_total:,.2f}")
        print(f"  Total labor hours: {v1_hours:.1f}  Total quantity: {v1_qty:.1f}")
    else:
        v1_total = 0.0
        print("  ✗ No rows found")

    # ── TEST 3: Budget from costCodes endpoint ────────────────────────────────
    print(f"\n[TEST 3] v2/costCodes/advancedRequest  (planned budget)")
    cc_all = fetch_all_pages(client, "POST", COST_CODES_URL, payload={
        "jobIds":      [JOB_ID],
        "costCodeIds": [cc_id],
        "limit":       500,
    })
    cc_match = next((c for c in cc_all if c.get("id") == cc_id), None)

    if cc_match:
        labor_bud = float(cc_match.get("laborDollars")       or 0)
        equip_bud = float(cc_match.get("equipmentDollars")   or 0)
        mat_bud   = float(cc_match.get("materialDollars")    or 0)
        sub_bud   = float(cc_match.get("subcontractDollars") or 0)
        total_bud = labor_bud + equip_bud + mat_bud + sub_bud
        planned_qty = float(cc_match.get("quantity") or cc_match.get("plannedQuantity") or 0)
        print(f"  Labor budget     : ${labor_bud:>12,.2f}")
        print(f"  Equipment budget : ${equip_bud:>12,.2f}")
        print(f"  Material budget  : ${mat_bud:>12,.2f}")
        print(f"  Subcontract bud  : ${sub_bud:>12,.2f}")
        print(f"  TOTAL BUDGET     : ${total_bud:>12,.2f}")
        print(f"  Planned qty      : {planned_qty:,.1f}")
        print(f"  Field names: {list(cc_match.keys())}")
    else:
        total_bud = 0.0
        print("  ✗ Cost code not found in costCodes response")

    # ── TEST 4: Math verification ─────────────────────────────────────────────
    hdiv()
    print("  MATH VERIFICATION")
    hdiv()
    if v2_rows and cc_match:
        util = round((v2_total / total_bud) * 100, 2) if total_bud > 0 else None
        util_str = f"{util}%" if util is not None else "N/A (no budget)"
        print(f"  Period actual  (v2 sum, {args.start}→{args.end}): ${v2_total:>12,.2f}")
        print(f"  Cumul. actual  (v1 all-time)                    : ${v1_total:>12,.2f}")
        print(f"  Planned budget                                  : ${total_bud:>12,.2f}")
        print(f"  Utilization (period / budget)                   : {util_str}")
        print(f"  Variance (budget - period actual)               : ${total_bud - v2_total:>12,.2f}")

        # Sanity check: v2 period sum should be ≤ v1 cumulative
        print()
        if v2_total <= v1_total + 0.01:
            print(f"  ✅ SANITY CHECK PASSED: period sum ({v2_total:,.2f}) ≤ cumulative ({v1_total:,.2f})")
        else:
            print(f"  ⚠️  SANITY CHECK FAILED: period sum ({v2_total:,.2f}) > cumulative ({v1_total:,.2f})")
            print(f"     This would mean date filter is wider than 'all time' — unexpected")

        # Check: if period is full project life, sums should be equal
        print()
        diff = abs(v2_total - v1_total)
        if diff < 0.05:
            print(f"  ✅ PERIOD ≈ CUMULATIVE: Suggests this date range covers full project history")
        else:
            print(f"  ℹ️  Period ${v2_total:,.2f} vs Cumulative ${v1_total:,.2f} — "
                  f"difference of ${diff:,.2f} means costs exist outside this window")

    # ── TEST 5: Field name cross-check ────────────────────────────────────────
    hdiv()
    print("  FIELD NAME AUDIT — confirming our fix reads the right keys")
    hdiv()
    if v2_rows:
        r = v2_rows[0]
        has_total_prefix = "totalLaborCost" in r
        has_plain        = "laborCost" in r
        print(f"  v2 row has 'totalLaborCost' key : {has_total_prefix}")
        print(f"  v2 row has 'laborCost' key      : {has_plain}")
        if has_plain and not has_total_prefix:
            print(f"  ✅ CONFIRMED: v2 uses plain keys (laborCost, equipmentCost etc.)")
            print(f"     Our fix correctly reads: row.get('totalLaborCost') OR row.get('laborCost')")
        elif has_total_prefix:
            print(f"  ℹ️  v2 DOES have total-prefixed keys — fix still works (reads totalLaborCost first)")

    if v1_rows:
        r = v1_rows[0]
        has_total_prefix = "totalLaborCost" in r
        has_plain        = "laborCost" in r
        print(f"\n  v1 row has 'totalLaborCost' key : {has_total_prefix}")
        print(f"  v1 row has 'laborCost' key      : {has_plain}")
        if has_total_prefix and not has_plain:
            print(f"  ✅ CONFIRMED: v1 uses total-prefixed keys (totalLaborCost etc.)")

    # ── Save full output ───────────────────────────────────────────────────────
    out = Path(__file__).parent / "verify_cost_code_output.json"
    out.write_text(json.dumps({
        "cost_code": cc_code,
        "cost_code_id": cc_id,
        "job_id": JOB_ID,
        "window": f"{args.start} → {args.end}",
        "summary": {
            "period_actual_cost":     round(v2_total, 2),
            "cumulative_actual_cost": round(v1_total, 2),
            "planned_budget":         round(total_bud, 2),
            "utilization_pct":        round((v2_total / total_bud * 100), 2) if total_bud > 0 else None,
            "variance":               round(total_bud - v2_total, 2),
        },
        "v2_period_rows": v2_rows,
        "v1_cumulative_rows": v1_rows,
        "budget_record": cc_match,
    }, indent=2, default=str))

    hdiv()
    print(f"\n  Full output saved → verify_cost_code_output.json")
    print(f"\n  WHAT TO CHECK IN HCSS PORTAL for {cc_code}:")
    print(f"    Set portal date filter: {args.start} → {args.end}")
    print(f"    'Actual All Cost' column should equal our period_actual_cost: ${v2_total:,.2f}")
    print(f"    'Budget' column should equal: ${total_bud:,.2f}")
    if total_bud > 0:
        print(f"    'Cost %' column should equal: {round(v2_total / total_bud * 100, 2)}%")
    print()


if __name__ == "__main__":
    main()
