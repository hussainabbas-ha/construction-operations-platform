"""
get_job_budget_vs_actual.py

Returns total Expected Budget and Actual Cost-to-Date per HCSS HeavyJob job.

CONFIRMED FINDINGS (exhaustive API probe, June 2026):

BUDGET  ->  GET /v1/costCodes?jobId=
    Sum: laborDollars + equipmentDollars + materialDollars
         + subcontractDollars + supplyDollars + customCostTypeDollars
    - advancedBudgets/material extended == costCodes.materialDollars (same data)
    - advancedBudgets/subcontract == costCodes.subcontractDollars (same data)
    - costCodes v1 alone is the correct and complete budget source.
    - Workers comp/burden is NOT in any API field. The portal total will be
      higher by that amount. This is a hard HCSS API limitation.

ACTUAL  ->  POST /v1/jobCostsToDate/search
    Sum: totalLaborCost + totalEquipmentCost + totalMaterialCost
         + totalSubcontractCost + totalTruckingCost
    - Cost code ID is NESTED: row["costCode"]["costCodeId"] (NOT top-level)
    - Field names use "total" prefix (different from /v1/jobCosts)
    - materialInstalled + subcontractWork are DOUBLE-COUNTS for all-time totals
    - jobCostCustomCosts entries are already inside jobCostsToDate rows

ACCURACY vs portal (verified 25006, 25048):
    Budget: exact match for jobs without burden (25048 exact, 25006 ~$606k under)
    Actual: within $0-$3,217 of portal (burden on actuals, not in API)

Usage:
    python scripts/get_job_budget_vs_actual.py --job-code 25001
    python scripts/get_job_budget_vs_actual.py --job-code 25001 25002 25003
    python scripts/get_job_budget_vs_actual.py --job-id <UUID>
    python scripts/get_job_budget_vs_actual.py --job-code 25001 --out results.json
    python scripts/get_job_budget_vs_actual.py --job-code 25001 --verbose
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from dotenv import load_dotenv
load_dotenv(_REPO_ROOT / ".env")

from payroll_verification.hcss_client import HCSSClient  # noqa: E402

logging.basicConfig(level=logging.WARNING, format="%(levelname)s  %(name)s  %(message)s")
logger = logging.getLogger(__name__)

_BASE          = "https://api.hcssapps.com/heavyjob/api/v1"
_COST_CODES_V1 = f"{_BASE}/costCodes"
_JOB_COSTS_TD  = f"{_BASE}/jobCostsToDate/search"
_JOBS_LIST     = f"{_BASE}/jobs"
_JOBS_ADVANCED = f"{_BASE}/jobs/advanced"


# ---------------------------------------------------------------------------
# Pagination helpers
# ---------------------------------------------------------------------------

def _paginate_get(client, url, params, *, label="", page_delay=0.3):
    rows, cursor = [], None
    while True:
        p = dict(params)
        if cursor:
            p["cursor"] = cursor
        raw = client._request("GET", url, params=p)
        batch, next_cursor = client._extract_results_and_cursor(raw)
        rows.extend(batch)
        if not next_cursor:
            break
        cursor = next_cursor
        if page_delay:
            time.sleep(page_delay)
    return rows


def _paginate_post(client, url, payload, *, label="", page_delay=0.3):
    rows, cursor = [], None
    while True:
        p = dict(payload)
        if cursor:
            p["cursor"] = cursor
        raw = client._request("POST", url, json=p)
        batch, next_cursor = client._extract_results_and_cursor(raw)
        rows.extend(batch)
        if not next_cursor:
            break
        cursor = next_cursor
        if page_delay:
            time.sleep(page_delay)
    return rows


# ---------------------------------------------------------------------------
# Step 1 -- Resolve job codes to UUIDs
# ---------------------------------------------------------------------------

def resolve_job_codes(client, job_codes):
    """Return {job_code: uuid} using GET /v1/jobs (paginated list of all jobs)."""
    if not job_codes:
        return {}
    print(f"\n[resolve] Looking up {len(job_codes)} job code(s): {', '.join(job_codes)}")
    rows = _paginate_get(client, _JOBS_LIST, {"limit": 500}, label="jobs list")
    target = {str(c).strip() for c in job_codes}
    code_to_uuid: dict[str, str] = {}
    for job in rows:
        code = str(job.get("code") or job.get("jobCode") or job.get("number") or "").strip()
        if code in target:
            uid = job.get("id") or job.get("jobId") or ""
            if uid:
                code_to_uuid[code] = uid
    for code in job_codes:
        if str(code).strip() not in code_to_uuid:
            print(f"  WARNING: job code '{code}' not found in /v1/jobs")
    print(f"  Resolved {len(code_to_uuid)}/{len(job_codes)} job code(s)")
    return code_to_uuid


def fetch_job_metadata(client, job_ids):
    """Return {uuid: {code, name}} via POST /v1/jobs/advanced (requires known UUIDs)."""
    if not job_ids:
        return {}
    try:
        raw = client._request("POST", _JOBS_ADVANCED, json={"jobIds": job_ids})
        job_list = raw if isinstance(raw, list) else raw.get("results", [])
    except Exception as exc:
        logger.warning("Could not fetch job metadata: %s", exc)
        return {}
    meta: dict[str, dict[str, str]] = {}
    for job in job_list:
        uid = job.get("id") or ""
        if uid:
            meta[uid] = {
                "code": str(job.get("code") or job.get("jobCode") or ""),
                "name": str(job.get("description") or job.get("name") or ""),
            }
    return meta


# ---------------------------------------------------------------------------
# Step 2 -- Fetch budget from GET /v1/costCodes
# ---------------------------------------------------------------------------

def fetch_budget(client, job_id, label):
    """
    GET /v1/costCodes?jobId=

    Formula (confirmed via exhaustive probe):
        laborDollars + equipmentDollars + materialDollars
        + subcontractDollars + supplyDollars + customCostTypeDollars_sum

    Uses v1 (not v2) because v1 includes supplyDollars.
    advancedBudgets/material and /subcontract are NOT additive --
    they return the same numbers already in materialDollars/subcontractDollars.
    Workers comp/burden is not available via any API field.
    """
    print(f"  [budget] Fetching costCodes for {label} ...")
    rows = _paginate_get(client, _COST_CODES_V1, {"jobId": job_id, "limit": 500})
    print(f"  [budget] {len(rows)} cost code(s)")

    total = 0.0
    cc_list: list[dict[str, Any]] = []

    for cc in rows:
        labor  = float(cc.get("laborDollars")       or 0)
        equip  = float(cc.get("equipmentDollars")   or 0)
        mat    = float(cc.get("materialDollars")    or 0)
        sub    = float(cc.get("subcontractDollars") or 0)
        supply = float(cc.get("supplyDollars")      or 0)

        custom_arr = cc.get("customCostTypeDollars") or []
        custom: float = 0.0
        if isinstance(custom_arr, list):
            custom = sum(
                float(item.get("dollars") or item.get("amount") or 0)
                for item in custom_arr
                if isinstance(item, dict)
            )
        elif isinstance(custom_arr, (int, float)):
            custom = float(custom_arr)

        cc_budget = labor + equip + mat + sub + supply + custom
        total += cc_budget
        cc_list.append({
            "costCodeId":        cc.get("id") or "",
            "costCode":          cc.get("code") or "",
            "description":       cc.get("description") or "",
            "expectedBudget":    round(cc_budget, 2),
            "laborBudget":       round(labor,  2),
            "equipmentBudget":   round(equip,  2),
            "materialBudget":    round(mat,    2),
            "subcontractBudget": round(sub,    2),
            "supplyBudget":      round(supply, 2),
            "customBudget":      round(custom, 2),
        })

    return round(total, 2), cc_list


# ---------------------------------------------------------------------------
# Step 3 -- Fetch actual cost from POST /v1/jobCostsToDate/search
# ---------------------------------------------------------------------------

def fetch_actual(client, job_id, label):
    """
    POST /v1/jobCostsToDate/search

    Confirmed correct -- this is the only source needed for all-time totals:
      - materialInstalled is a DOUBLE-COUNT (already inside jobCostsToDate)
      - subcontractWork is a DOUBLE-COUNT for all-time totals
      - jobCostCustomCosts entries are already aggregated inside jobCostsToDate

    Field names on this endpoint use "total" prefix:
        totalLaborCost, totalEquipmentCost, totalMaterialCost,
        totalSubcontractCost, totalTruckingCost
    Cost code ID is NESTED: row["costCode"]["costCodeId"] -- NOT top-level.
    """
    print(f"  [actual]  Fetching jobCostsToDate for {label} ...")
    rows = _paginate_post(client, _JOB_COSTS_TD, {"jobIds": [job_id], "limit": 500})
    print(f"  [actual]  {len(rows)} record(s)")

    cc_map: dict[str, dict[str, float]] = {}
    total = 0.0

    for row in rows:
        cc_obj = row.get("costCode") or {}
        ccid = (
            cc_obj.get("costCodeId")
            or cc_obj.get("id")
            or row.get("costCodeId")
            or ""
        )
        labor = float(row.get("totalLaborCost")       or row.get("laborCost")       or 0)
        equip = float(row.get("totalEquipmentCost")   or row.get("equipmentCost")   or 0)
        mat   = float(row.get("totalMaterialCost")    or row.get("materialCost")    or 0)
        sub   = float(row.get("totalSubcontractCost") or row.get("subcontractCost") or 0)
        truck = float(row.get("totalTruckingCost")    or row.get("truckingCost")    or 0)
        actual = labor + equip + mat + sub + truck
        total += actual

        if ccid:
            if ccid in cc_map:
                cc_map[ccid]["actualCost"]      += actual
                cc_map[ccid]["laborCost"]       += labor
                cc_map[ccid]["equipmentCost"]   += equip
                cc_map[ccid]["materialCost"]    += mat
                cc_map[ccid]["subcontractCost"] += sub
                cc_map[ccid]["truckingCost"]    += truck
            else:
                cc_map[ccid] = {
                    "actualCost": actual, "laborCost": labor,
                    "equipmentCost": equip, "materialCost": mat,
                    "subcontractCost": sub, "truckingCost": truck,
                }

    total = round(total, 2)
    print(f"  [actual]  total = ${total:,.2f}")
    return total, cc_map


# ---------------------------------------------------------------------------
# Step 4 -- Join budget + actual into per-cost-code rows
# ---------------------------------------------------------------------------

def build_cost_code_rows(budget_list, actual_map):
    rows: list[dict[str, Any]] = []
    budgeted_ids: set[str] = set()

    for b in budget_list:
        ccid = b["costCodeId"]
        budgeted_ids.add(ccid)
        act = actual_map.get(ccid, {})
        actual = act.get("actualCost", 0.0)
        budget = b["expectedBudget"]
        variance = round(budget - actual, 2)
        pct = round(actual / budget * 100, 1) if budget > 0 else None
        rows.append({
            "costCodeId":        ccid,
            "costCode":          b["costCode"],
            "description":       b["description"],
            "expectedBudget":    budget,
            "actualCost":        round(actual, 2),
            "variance":          variance,
            "percentSpent":      pct,
            "laborBudget":       b["laborBudget"],
            "equipmentBudget":   b["equipmentBudget"],
            "materialBudget":    b["materialBudget"],
            "subcontractBudget": b["subcontractBudget"],
            "supplyBudget":      b["supplyBudget"],
            "customBudget":      b["customBudget"],
            "laborActual":       round(act.get("laborCost",       0), 2),
            "equipmentActual":   round(act.get("equipmentCost",   0), 2),
            "materialActual":    round(act.get("materialCost",    0), 2),
            "subcontractActual": round(act.get("subcontractCost", 0), 2),
            "truckingActual":    round(act.get("truckingCost",    0), 2),
        })

    # Unbudgeted cost codes (spend exists but not in the plan)
    for ccid, act in actual_map.items():
        if ccid in budgeted_ids:
            continue
        actual = act.get("actualCost", 0.0)
        if actual == 0:
            continue
        rows.append({
            "costCodeId": ccid, "costCode": "", "description": "(unbudgeted)",
            "expectedBudget": 0.0, "actualCost": round(actual, 2),
            "variance": round(-actual, 2), "percentSpent": None,
            "laborBudget": 0.0, "equipmentBudget": 0.0, "materialBudget": 0.0,
            "subcontractBudget": 0.0, "supplyBudget": 0.0, "customBudget": 0.0,
            "laborActual":       round(act.get("laborCost",       0), 2),
            "equipmentActual":   round(act.get("equipmentCost",   0), 2),
            "materialActual":    round(act.get("materialCost",    0), 2),
            "subcontractActual": round(act.get("subcontractCost", 0), 2),
            "truckingActual":    round(act.get("truckingCost",    0), 2),
        })

    rows.sort(key=lambda r: (r["costCode"] or r["costCodeId"]))
    return rows


# ---------------------------------------------------------------------------
# Step 5 -- Process a single job
# ---------------------------------------------------------------------------

def _derive_status(budget, actual):
    if budget <= 0:
        return "NO_BUDGET"
    pct = actual / budget * 100
    if pct >= 100: return "OVER_BUDGET"
    if pct >= 90:  return "OVER_RISK"
    if pct >= 75:  return "NEAR_LIMIT"
    return "ON_TRACK"


def process_job(client, job_id, job_code, job_name):
    label = job_code or job_id[:8]
    sep = "-" * 62
    print(f"\n{sep}")
    print(f"  Job: {job_code}  |  {job_name}  |  {job_id}")
    print(sep)

    total_budget, budget_list = fetch_budget(client, job_id, label)
    total_actual, actual_map  = fetch_actual(client, job_id, label)

    variance  = round(total_budget - total_actual, 2)
    pct_spent = round(total_actual / total_budget * 100, 1) if total_budget > 0 else None
    status    = _derive_status(total_budget, total_actual)
    cc_rows   = build_cost_code_rows(budget_list, actual_map)

    # Console summary box (ASCII only)
    print(f"\n  +{'-'*58}+")
    print(f"  | SUMMARY: {label:<48}|")
    print(f"  +{'-'*58}+")
    print(f"  | {'Base Budget (API):':<26} {'$' + f'{total_budget:,.2f}':>29} |")
    print(f"  | {'Actual Cost:':<26} {'$' + f'{total_actual:,.2f}':>29} |")
    print(f"  | {'Variance:':<26} {'$' + f'{variance:,.2f}':>29} |")
    if pct_spent is not None:
        print(f"  | {'Percent Spent:':<26} {f'{pct_spent:.1f}%':>29} |")
    print(f"  | {'Status:':<26} {status:>29} |")
    print(f"  | {'Cost Codes:':<26} {len(cc_rows):>29,} |")
    print(f"  +{'-'*58}+")
    print(f"  | NOTE: Budget = labor+equip+mat+sub+supply (API fields) |")
    print(f"  | Workers comp/burden excluded -- not in HCSS API.       |")
    print(f"  +{'-'*58}+")

    return {
        "jobId":               job_id,
        "jobCode":             job_code,
        "jobName":             job_name,
        "totalExpectedBudget": total_budget,
        "totalActualCost":     total_actual,
        "variance":            variance,
        "percentSpent":        pct_spent,
        "status":              status,
        "costCodeCount":       len(cc_rows),
        "budgetNote": (
            "totalExpectedBudget = laborDollars+equipmentDollars+materialDollars"
            "+subcontractDollars+supplyDollars+customCostTypeDollars from "
            "/v1/costCodes. Workers comp/burden NOT in any HCSS API field; "
            "portal 'Budgeted All Cost' is higher by that burden amount. "
            "totalActualCost from /v1/jobCostsToDate/search."
        ),
        "costCodes": cc_rows,
    }


# ---------------------------------------------------------------------------
# Verbose cost-code table
# ---------------------------------------------------------------------------

def print_verbose(result):
    rows = result.get("costCodes", [])
    if not rows:
        return
    print(f"\n  Cost Code Detail -- {result.get('jobCode', '')}")
    print(f"  {'Cost Code':<14} {'Description':<34} {'Budget':>14} {'Actual':>14} {'Variance':>14} {'%':>7}")
    print("  " + "-" * 104)
    for r in rows:
        pct = f"{r['percentSpent']:.1f}%" if r["percentSpent"] is not None else "N/A"
        print(
            f"  {r['costCode']:<14} "
            f"{r['description'][:33]:<34} "
            f"${r['expectedBudget']:>13,.2f} "
            f"${r['actualCost']:>13,.2f} "
            f"${r['variance']:>13,.2f} "
            f"{pct:>7}"
        )
    print("  " + "-" * 104)
    print(
        f"  {'TOTAL':<14} {'':<34} "
        f"${result['totalExpectedBudget']:>13,.2f} "
        f"${result['totalActualCost']:>13,.2f} "
        f"${result['variance']:>13,.2f}"
    )


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def run(args):
    client = HCSSClient()

    job_ids_raw: list[str] = list(args.job_id or [])
    id_to_code: dict[str, str] = {}
    id_to_name: dict[str, str] = {}

    if args.job_code:
        code_map = resolve_job_codes(client, list(args.job_code))
        for code, uid in code_map.items():
            job_ids_raw.append(uid)
            id_to_code[uid] = code

    seen: set[str] = set()
    job_ids: list[str] = []
    for jid in (j.strip() for j in job_ids_raw):
        if jid and jid not in seen:
            seen.add(jid)
            job_ids.append(jid)

    if not job_ids:
        print("\nERROR: No job IDs. Provide --job-id or --job-code.")
        sys.exit(1)

    need_meta = [j for j in job_ids if j not in id_to_name]
    if need_meta:
        meta = fetch_job_metadata(client, need_meta)
        for uid, m in meta.items():
            id_to_code.setdefault(uid, m["code"])
            id_to_name[uid] = m["name"]

    results: list[dict[str, Any]] = []
    for idx, jid in enumerate(job_ids, 1):
        code = id_to_code.get(jid, "")
        name = id_to_name.get(jid, "")
        try:
            result = process_job(client, jid, code, name)
        except Exception as exc:
            print(f"\n  ERROR processing {code or jid}: {exc}")
            logger.exception("Failed: %s", jid)
            result = {"jobId": jid, "jobCode": code, "jobName": name, "error": str(exc)}
        results.append(result)
        if args.verbose and "error" not in result:
            print_verbose(result)
        if idx < len(job_ids):
            time.sleep(0.5)

    ok = [r for r in results if "error" not in r]
    if len(ok) > 1:
        gb = sum(r["totalExpectedBudget"] for r in ok)
        ga = sum(r["totalActualCost"]     for r in ok)
        gv = round(gb - ga, 2)
        gp = round(ga / gb * 100, 1) if gb > 0 else None
        print(f"\n  {'='*52}")
        print(f"  GRAND TOTAL  ({len(ok)} jobs)")
        print(f"  {'-'*52}")
        print(f"  Expected Budget  :  ${gb:>27,.2f}")
        print(f"  Actual Cost      :  ${ga:>27,.2f}")
        print(f"  Variance         :  ${gv:>27,.2f}")
        if gp is not None:
            print(f"  Percent Spent    :  {gp:>27.1f}%")
        print(f"  {'='*52}")

    return results


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Get total Expected Budget and Actual Cost-to-Date from HCSS.",
    )
    parser.add_argument("--job-id",   nargs="+", metavar="UUID",
                        help="One or more HCSS job UUIDs")
    parser.add_argument("--job-code", nargs="+", metavar="CODE",
                        help="One or more job codes, e.g. 25001")
    parser.add_argument("--out",      default=None, metavar="FILE",
                        help="Save full JSON results to this file")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Print per-cost-code breakdown table")
    parser.add_argument("--debug",    action="store_true",
                        help="Enable debug logging")
    args = parser.parse_args()

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    if not args.job_id and not args.job_code:
        parser.print_help()
        sys.exit(1)

    print("\n" + "=" * 62)
    print("  HCSS Job Budget vs Actual Cost")
    print("=" * 62)

    results = run(args)

    if args.out:
        out_path = Path(args.out)
        out_path.write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")
        print(f"\n  Results saved -> {out_path.resolve()}")
    else:
        summary = [
            {
                "jobId":               r.get("jobId"),
                "jobCode":             r.get("jobCode"),
                "jobName":             r.get("jobName"),
                "totalExpectedBudget": r.get("totalExpectedBudget"),
                "totalActualCost":     r.get("totalActualCost"),
                "variance":            r.get("variance"),
                "percentSpent":        r.get("percentSpent"),
                "status":              r.get("status"),
            }
            for r in results
        ]
        print("\n  JSON Summary:")
        print(json.dumps(summary, indent=2, default=str))

    print("\n  Done.\n")


if __name__ == "__main__":
    main()
