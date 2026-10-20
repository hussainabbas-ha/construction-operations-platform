"""
verify_expected_budget.py
══════════════════════════════════════════════════════════════════════════════
Verify the per-cost-code expected budget formula at the JOB level.

Formula (per cost code, then summed):
    completion_pct     = actual_quantity / budgeted_quantity * 100
    expected_budget_cc = budgeted_cost   * (completion_pct / 100)
    job_expected_budget = SUM(expected_budget_cc for all cost codes)

Data sources (all live from HCSS — no DB):
    budgeted_quantity  → GET  /v1/costCodes?jobId=               field: quantity
    budgeted_cost      → GET  /v1/costCodes?jobId=               sum: laborDollars+equipDollars+matDollars+subDollars+supplyDollars+customCostTypeDollars
    actual_quantity    → POST /v1/costCode/progress/advancedRequest  MAX(toDateQuantity) per cost code, no date filter
    actual_cost        → POST /v1/jobCostsToDate/search          sum: totalLaborCost+totalEquipCost+totalMatCost+totalSubCost+totalTruckCost

WHY progress toDateQuantity for actual quantity:
    Confirmed by verify_quantity_fix.py to match HCSS portal exactly.
    jobCostsToDate returns one row per cost TYPE per cost code — summing
    its quantity field multi-counts. toDateQuantity on progress is the
    single correct cumulative snapshot.

Usage:
    python scripts/verify_expected_budget.py --job-code 25006
    python scripts/verify_expected_budget.py --job-code 25006 25048
    python scripts/verify_expected_budget.py --job-id <UUID>
    python scripts/verify_expected_budget.py --job-code 25006 --verbose
    python scripts/verify_expected_budget.py --job-code 25006 --out result.json
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

logging.basicConfig(level=logging.WARNING, format="%(levelname)s  %(message)s")
logger = logging.getLogger(__name__)

_BASE          = "https://api.hcssapps.com/heavyjob/api"
_JOBS_LIST     = f"{_BASE}/v1/jobs"
_JOBS_ADV      = f"{_BASE}/v1/jobs/advanced"
_COST_CODES_V1 = f"{_BASE}/v1/costCodes"          # v1 — includes supplyDollars + customCostTypeDollars
_JOB_COSTS_TD  = f"{_BASE}/v1/jobCostsToDate/search"

# Budget dollar fields — v1 endpoint (confirmed in get_job_budget_vs_actual.py + hcss_client.py)
# supplyDollars is ONLY in v1, not v2 — critical for correct totals matching HCSS portal
_BUDGET_FIELDS = ["laborDollars", "equipmentDollars", "materialDollars", "subcontractDollars", "supplyDollars"]


# ── Helpers ───────────────────────────────────────────────────────────────────

def _sf(val: Any) -> float:
    try:
        return float(val or 0)
    except (TypeError, ValueError):
        return 0.0


def _with_retry(fn, *, label: str = "", max_retries: int = 8) -> Any:
    """Call fn(), retrying on 429 with exponential back-off."""
    import requests as _req
    retries = 0
    while True:
        try:
            return fn()
        except _req.exceptions.HTTPError as exc:
            code = exc.response.status_code if exc.response is not None else None
            if code == 429 and retries < max_retries:
                ra = exc.response.headers.get("Retry-After") if exc.response else None
                wait = float(ra) if ra else min(120.0, (2 ** retries) * 5.0)
                retries += 1
                print(f"  [429] {label} — waiting {wait:.0f}s (retry {retries}/{max_retries}) ...")
                time.sleep(wait)
                continue
            raise


def _post_pages(client: HCSSClient, url: str, payload: dict, *, label: str, delay: float = 1.0) -> list[dict]:
    rows, cursor = [], None
    page = 0
    while True:
        req = dict(payload)
        if cursor:
            req["cursor"] = cursor
        raw = _with_retry(lambda r=req: client._request("POST", url, json=r), label=label)
        batch, next_cursor = client._extract_results_and_cursor(raw)
        page += 1
        rows.extend(batch)
        logger.debug("[%s] page %d  rows=%d  total=%d", label, page, len(batch), len(rows))
        if not next_cursor:
            break
        cursor = next_cursor
        time.sleep(delay)
    return rows


def _get_pages(client: HCSSClient, url: str, params: dict, *, label: str, delay: float = 0.5) -> list[dict]:
    rows, cursor = [], None
    while True:
        p = dict(params)
        if cursor:
            p["cursor"] = cursor
        raw = _with_retry(lambda pr=p: client._request("GET", url, params=pr), label=label)
        batch, next_cursor = client._extract_results_and_cursor(raw)
        rows.extend(batch)
        if not next_cursor:
            break
        cursor = next_cursor
        time.sleep(delay)
    return rows


# ── Job resolution ────────────────────────────────────────────────────────────

def resolve_codes(client: HCSSClient, codes: list[str]) -> dict[str, str]:
    """Return {job_code: uuid}."""
    print(f"\n[1] Resolving {len(codes)} job code(s): {', '.join(codes)}")
    rows = _get_pages(client, _JOBS_LIST, {"limit": 500}, label="jobs list")
    target = {str(c).strip() for c in codes}
    out: dict[str, str] = {}
    for job in rows:
        code = str(job.get("code") or job.get("jobCode") or "").strip()
        if code in target:
            uid = job.get("id") or ""
            if uid:
                out[code] = uid
    for c in codes:
        if c not in out:
            print(f"  WARNING: '{c}' not found in /v1/jobs")
    print(f"  Resolved {len(out)}/{len(codes)}")
    time.sleep(2)  # cool-down before next call
    return out


def fetch_meta(client: HCSSClient, job_ids: list[str]) -> dict[str, dict]:
    """Return {uuid: {code, name}}."""
    try:
        raw = _with_retry(
            lambda: client._request("POST", _JOBS_ADV, json={"jobIds": job_ids}),
            label="jobs/advanced",
        )
        lst = raw if isinstance(raw, list) else raw.get("results", [])
    except Exception as exc:
        logger.warning("jobs/advanced failed: %s", exc)
        return {}
    out: dict[str, dict] = {}
    for j in lst:
        uid = j.get("id") or ""
        if uid:
            out[uid] = {
                "code": str(j.get("code") or j.get("jobCode") or ""),
                "name": str(j.get("description") or j.get("name") or ""),
            }
    time.sleep(2)
    return out


# ── Step 1: Budgeted quantity + budgeted cost per cost code ───────────────────

def fetch_cost_codes(client: HCSSClient, job_id: str, label: str) -> list[dict]:
    """
    GET /v1/costCodes?jobId=   (v1, NOT v2)

    WHY v1 and not v2:
        v1 includes supplyDollars which v2 omits entirely.
        Confirmed in budget_tracking/hcss_client.py and get_job_budget_vs_actual.py.
        Cost codes like Bond, Indirect, Barrier Rental store their budget in
        supplyDollars — v2 returns 0 for those, causing large mismatches.

    budgeted_cost = laborDollars + equipmentDollars + materialDollars
                  + subcontractDollars + supplyDollars + customCostTypeDollars

    budgeted_qty  = quantity field (same on both v1 and v2)
    """
    print(f"  [cost codes] fetching for {label} (v1 — includes supplyDollars) ...")
    rows = _get_pages(client, _COST_CODES_V1, {"jobId": job_id, "limit": 500},
                      label=f"cost codes {label}")
    print(f"  [cost codes] {len(rows)} returned")

    out = []
    for cc in rows:
        bq = _sf(cc.get("quantity") or cc.get("plannedQuantity") or cc.get("budgetedQuantity"))

        # Sum all standard fields
        bc = sum(_sf(cc.get(f)) for f in _BUDGET_FIELDS)

        # Add customCostTypeDollars — it's an array of {dollars: X} objects
        custom_arr = cc.get("customCostTypeDollars") or []
        if isinstance(custom_arr, list):
            bc += sum(
                _sf(item.get("dollars") or item.get("amount") or 0)
                for item in custom_arr
                if isinstance(item, dict)
            )
        elif isinstance(custom_arr, (int, float)):
            bc += _sf(custom_arr)

        out.append({
            "cost_code_id":  cc.get("id") or "",
            "cost_code":     cc.get("code") or cc.get("costCode") or "",
            "description":   cc.get("description") or cc.get("name") or "",
            "unit":          cc.get("unit") or cc.get("unitOfMeasure") or "",
            "budgeted_qty":  bq,
            "budgeted_cost": bc,
        })
    return out


# ── Step 2: Actual quantity + actual cost per cost code ──────────────────────

def fetch_actual_qty_and_cost(client: HCSSClient, job_id: str, label: str) -> tuple[dict[str, float], dict[str, float]]:
    """
    Two separate calls — each is the confirmed correct source for its value.

    ACTUAL QUANTITY → POST /v1/costCode/progress/advancedRequest (NO date filter)
        Field: toDateQuantity — cumulative quantity to date per cost code.
        Strategy: take MAX(toDateQuantity) per cost_code_id across all records.
        This is what verify_quantity_fix.py confirmed matches HCSS portal exactly.
        Do NOT use jobCostsToDate for qty — that endpoint returns one row per
        cost TYPE (labor, equipment, etc.) per cost code and summing totalQuantity
        across those rows multi-counts the same quantity.

    ACTUAL COST → POST /v1/jobCostsToDate/search
        Cumulative all-time actual cost per cost code.
        costCode ID is NESTED: row["costCode"]["costCodeId"]
        Field names have "total" prefix on this endpoint.
        Confirmed in get_job_budget_vs_actual.py.
    """
    # ── Actual quantity from progress endpoint ────────────────────────────────
    _PROGRESS_V1 = "https://api.hcssapps.com/heavyjob/api/v1/costCode/progress/advancedRequest"
    print(f"  [actual qty]  fetching progress (no date filter) for {label} ...")
    progress_rows = _post_pages(
        client, _PROGRESS_V1,
        {"jobId": job_id, "limit": 500},
        label=f"progress {label}",
        delay=1.0,
    )
    print(f"  [actual qty]  {len(progress_rows)} progress records")

    # Take MAX(toDateQuantity) per cost code — most recent cumulative snapshot
    # Confirmed by verify_quantity_fix.py to match HCSS portal
    qty_map: dict[str, float] = {}
    for p in progress_rows:
        ccid = p.get("costCodeId") or p.get("id") or ""
        if not ccid:
            continue
        qty = _sf(p.get("toDateQuantity") or p.get("quantityToDate") or 0)
        if qty > qty_map.get(ccid, 0.0):
            qty_map[ccid] = qty

    time.sleep(2)

    # ── Actual cost from jobCostsToDate ───────────────────────────────────────
    print(f"  [actual cost] fetching jobCostsToDate for {label} ...")
    cost_rows = _post_pages(
        client, _JOB_COSTS_TD,
        {"jobIds": [job_id], "limit": 500},
        label=f"jobCostsToDate {label}",
    )
    print(f"  [actual cost] {len(cost_rows)} records")

    cost_map: dict[str, float] = {}
    for row in cost_rows:
        cc_obj = row.get("costCode") or {}
        ccid = (cc_obj.get("costCodeId") or cc_obj.get("id")
                or row.get("costCodeId") or "")
        if not ccid:
            continue
        cost = (
            _sf(row.get("totalLaborCost")         or row.get("laborCost"))
            + _sf(row.get("totalEquipmentCost")   or row.get("equipmentCost"))
            + _sf(row.get("totalMaterialCost")    or row.get("materialCost"))
            + _sf(row.get("totalSubcontractCost") or row.get("subcontractCost"))
            + _sf(row.get("totalTruckingCost")    or row.get("truckingCost"))
        )
        cost_map[ccid] = cost_map.get(ccid, 0.0) + cost

    return qty_map, cost_map


# ── Step 4: Apply formula per cost code, sum for job ─────────────────────────

def compute_expected_budget(
    cc_list: list[dict],
    actual_qty_map: dict[str, float],
    actual_cost_map: dict[str, float],
) -> tuple[list[dict], dict]:
    """
    Per cost code:
        completion_pct     = actual_qty / budgeted_qty * 100
        expected_budget_cc = budgeted_cost * (completion_pct / 100)

    Job totals:
        total_expected_budget = SUM(expected_budget_cc)
        total_actual_cost     = SUM(actual_cost_cc)
        total_budgeted_cost   = SUM(budgeted_cost_cc)
    """
    rows = []
    total_budgeted_cost   = 0.0
    total_expected_budget = 0.0
    total_actual_cost     = 0.0
    skipped_no_qty        = 0
    skipped_zero_budget   = 0

    for cc in cc_list:
        ccid        = cc["cost_code_id"]
        bq          = cc["budgeted_qty"]
        bc          = cc["budgeted_cost"]
        actual_qty  = actual_qty_map.get(ccid, 0.0)
        actual_cost = actual_cost_map.get(ccid, 0.0)

        total_budgeted_cost += bc
        total_actual_cost   += actual_cost

        # Skip cost codes with no quantity budget — can't compute completion
        if bq <= 0:
            skipped_no_qty += 1
            rows.append({**cc,
                "actual_qty": actual_qty,
                "actual_cost": actual_cost,
                "completion_pct": None,
                "expected_budget": 0.0,
                "variance": -actual_cost,
                "note": "skipped — no budgeted quantity",
            })
            continue

        # Skip cost codes with zero dollar budget
        if bc <= 0:
            skipped_zero_budget += 1
            rows.append({**cc,
                "actual_qty": actual_qty,
                "actual_cost": actual_cost,
                "completion_pct": actual_qty / bq * 100,
                "expected_budget": 0.0,
                "variance": -actual_cost,
                "note": "skipped — zero dollar budget",
            })
            continue

        completion_pct    = actual_qty / bq * 100
        expected_budget   = bc * (completion_pct / 100)
        variance          = expected_budget - actual_cost

        total_expected_budget += expected_budget

        rows.append({**cc,
            "actual_qty":      actual_qty,
            "actual_cost":     actual_cost,
            "completion_pct":  completion_pct,
            "expected_budget": expected_budget,
            "variance":        variance,
            "note": "",
        })

    rows.sort(key=lambda r: r["cost_code"] or r["cost_code_id"])

    totals = {
        "total_budgeted_cost":   total_budgeted_cost,
        "total_expected_budget": total_expected_budget,
        "total_actual_cost":     total_actual_cost,
        "total_variance":        total_expected_budget - total_actual_cost,
        "cost_codes_total":      len(cc_list),
        "cost_codes_with_qty":   len(cc_list) - skipped_no_qty,
        "skipped_no_qty_budget": skipped_no_qty,
        "skipped_zero_dollar":   skipped_zero_budget,
    }
    return rows, totals


# ── Process one job ───────────────────────────────────────────────────────────

def process_job(client: HCSSClient, job_id: str, job_code: str, job_name: str) -> dict:
    lbl = job_code or job_id[:8]
    sep = "─" * 66
    print(f"\n{sep}")
    print(f"  Job : {job_code}  |  {job_name}")
    print(f"  ID  : {job_id}")
    print(sep)

    cc_list                    = fetch_cost_codes(client, job_id, lbl)
    time.sleep(2)
    actual_qty_map, actual_cost_map = fetch_actual_qty_and_cost(client, job_id, lbl)

    rows, totals = compute_expected_budget(cc_list, actual_qty_map, actual_cost_map)

    w = 62
    print(f"\n  ╔{'═'*w}╗")
    print(f"  ║ {'EXPECTED BUDGET SUMMARY':^{w}} ║")
    print(f"  ║ {lbl:^{w}} ║")
    print(f"  ╠{'═'*w}╣")
    _bc = "$" + f"{totals['total_budgeted_cost']:,.2f}"
    _eb = "$" + f"{totals['total_expected_budget']:,.2f}"
    _ac = "$" + f"{totals['total_actual_cost']:,.2f}"
    _va = "$" + f"{totals['total_variance']:,.2f}"
    print(f"  ║  {'Total Budgeted Cost (all CCs):':<36} {_bc:>23} ║")
    print(f"  ║  {'Total Expected Budget (formula):':<36} {_eb:>23} ║")
    print(f"  ║  {'Total Actual Cost (to date):':<36} {_ac:>23} ║")
    print(f"  ║  {'Variance (expected − actual):':<36} {_va:>23} ║")
    print(f"  ╠{'═'*w}╣")
    print(f"  ║  {'Total cost codes:':<36} {totals['cost_codes_total']:>23,} ║")
    print(f"  ║  {'  with qty budget (formula applied):':<36} {totals['cost_codes_with_qty']:>23,} ║")
    print(f"  ║  {'  skipped (no qty budget):':<36} {totals['skipped_no_qty_budget']:>23,} ║")
    print(f"  ╠{'═'*w}╣")
    print(f"  ║  Formula: expected_cc = (actual_qty/budget_qty)*budget_cost  ║")
    print(f"  ║  Sum of all per-CC expected = job expected_budget             ║")
    print(f"  ╚{'═'*w}╝")

    return {
        "job_id":   job_id,
        "job_code": job_code,
        "job_name": job_name,
        "totals":   totals,
        "cost_codes": rows,
        "sources": {
            "budgeted_qty_and_cost": "POST /v2/costCodes/advancedRequest",
            "actual_qty": "POST /v1/costCode/progress/advancedRequest (no date filter, SUM quantity)",
            "actual_cost": "POST /v1/jobCostsToDate/search (cumulative)",
        },
    }


# ── Verbose table ─────────────────────────────────────────────────────────────

def print_verbose(result: dict) -> None:
    rows = [r for r in result["cost_codes"] if not r.get("note")]
    if not rows:
        return
    t = result["totals"]
    print(f"\n  Detail — {result['job_code']} "
          f"({len(rows)} cost codes with qty budget, "
          f"{t['skipped_no_qty_budget']} skipped)")
    hdr = f"  {'CC':<14} {'Description':<30} {'Unit':<5} {'BudgetQty':>11} {'ActualQty':>11} {'Compl%':>7} {'BudgetCost':>13} {'Expected':>13} {'Actual':>13} {'Variance':>13}"
    print(hdr)
    print("  " + "─" * len(hdr.rstrip()))
    for r in rows:
        pct = f"{r['completion_pct']:.1f}%" if r["completion_pct"] is not None else "N/A"
        print(
            f"  {r['cost_code']:<14}"
            f" {r['description'][:29]:<30}"
            f" {r['unit']:<5}"
            f" {r['budgeted_qty']:>11,.3f}"
            f" {r['actual_qty']:>11,.3f}"
            f" {pct:>7}"
            f" ${r['budgeted_cost']:>12,.2f}"
            f" ${r['expected_budget']:>12,.2f}"
            f" ${r['actual_cost']:>12,.2f}"
            f" ${r['variance']:>12,.2f}"
        )
    print("  " + "─" * len(hdr.rstrip()))
    print(
        f"  {'TOTAL':<14} {'':<30} {'':<5} {'':<11} {'':<11} {'':<7}"
        f" ${t['total_budgeted_cost']:>12,.2f}"
        f" ${t['total_expected_budget']:>12,.2f}"
        f" ${t['total_actual_cost']:>12,.2f}"
        f" ${t['total_variance']:>12,.2f}"
    )


# ── Orchestration ─────────────────────────────────────────────────────────────

def run(args: argparse.Namespace) -> list[dict]:
    client = HCSSClient()

    id_to_code: dict[str, str] = {}
    id_to_name: dict[str, str] = {}
    job_ids_raw: list[str] = list(args.job_id or [])

    if args.job_code:
        code_map = resolve_codes(client, list(args.job_code))
        for code, uid in code_map.items():
            job_ids_raw.append(uid)
            id_to_code[uid] = code

    # Deduplicate
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
        meta = fetch_meta(client, need_meta)
        for uid, m in meta.items():
            id_to_code.setdefault(uid, m["code"])
            id_to_name[uid] = m["name"]

    results: list[dict] = []
    for idx, jid in enumerate(job_ids, 1):
        code = id_to_code.get(jid, "")
        name = id_to_name.get(jid, "")
        try:
            result = process_job(client, jid, code, name)
        except Exception as exc:
            print(f"\n  ERROR on {code or jid}: {exc}")
            logger.exception("Failed: %s", jid)
            result = {"job_id": jid, "job_code": code, "job_name": name, "error": str(exc)}
        results.append(result)

        if args.verbose and "error" not in result:
            print_verbose(result)

        if idx < len(job_ids):
            print(f"\n  Pausing 3s before next job ...")
            time.sleep(3)

    # Grand total for multi-job runs
    ok = [r for r in results if "error" not in r]
    if len(ok) > 1:
        gb = sum(r["totals"]["total_budgeted_cost"]   for r in ok)
        ge = sum(r["totals"]["total_expected_budget"] for r in ok)
        ga = sum(r["totals"]["total_actual_cost"]     for r in ok)
        gv = ge - ga
        print(f"\n  {'═'*54}")
        print(f"  GRAND TOTAL  ({len(ok)} jobs)")
        print(f"  {'─'*54}")
        print(f"  Total Budgeted Cost    : ${gb:>27,.2f}")
        print(f"  Total Expected Budget  : ${ge:>27,.2f}")
        print(f"  Total Actual Cost      : ${ga:>27,.2f}")
        print(f"  Total Variance         : ${gv:>27,.2f}")
        print(f"  {'═'*54}")

    return results


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Verify job-level Expected Budget using per-cost-code formula:\n"
            "  expected_cc = (actual_qty / budgeted_qty) * budgeted_cost\n"
            "  job_expected = SUM(expected_cc)\n\n"
            "Compare the printed 'Total Expected Budget' against HCSS portal."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--job-id",   nargs="+", metavar="UUID",  help="One or more HCSS job UUIDs")
    parser.add_argument("--job-code", nargs="+", metavar="CODE",  help="One or more job codes e.g. 25006")
    parser.add_argument("--verbose",  "-v", action="store_true",  help="Print per-cost-code breakdown table")
    parser.add_argument("--out",      default=None, metavar="FILE", help="Save full JSON to file")
    parser.add_argument("--debug",    action="store_true",        help="Enable debug logging")
    args = parser.parse_args()

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    if not args.job_id and not args.job_code:
        parser.print_help()
        sys.exit(1)

    print("\n" + "═" * 66)
    print("  HCSS Expected Budget Verification")
    print("  Formula: SUM[ (actual_qty/budget_qty) * budget_cost ] per CC")
    print("═" * 66)

    results = run(args)

    if args.out:
        out = Path(args.out)
        out.write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")
        print(f"\n  Saved → {out.resolve()}")
    else:
        summary = [
            {k: r.get(k) for k in ("job_id", "job_code", "job_name", "totals", "error")}
            for r in results
        ]
        print("\n  JSON Summary:")
        print(json.dumps(summary, indent=2, default=str))

    print("\n  Done.\n")


if __name__ == "__main__":
    main()
