"""
get_job_expected_all_cost.py

Returns Expected All Cost, Actual Cost, and Variance for HCSS HeavyJob jobs.

Expected All Cost (NOT Budgeted All Cost) — matches HCSS portal Cost Report:

    Per cost code (then summed):
        T&M (isTm=true):           expected = actual cost
        Normal with qty budget:    formula = (toDateQty / budgetQty) * budgetedAllCost
        Cap Expected (isCapExpected, default true):
            expected = min(formula, budgetedAllCost)
            Prevents expected exceeding budget when qty > 100% complete.
        No qty/budget:             expected = 0

    HCSS docs: https://help.hcss.com/s/article/How-is-Expected-Actual-and-Variance-calculated
    Cap Expected: https://help.hcss.com/s/article/How-to-Set-Cost-Codes-to-Cap-Expected-in-HeavyJob-Web-Only

Actual Cost + production qty  ->  GET /v1/jobs/{jobId}/costs  (single call)
    Per cost code actual: laborCost + equipmentCost + materialCost + subcontractCost
                          + truckingCost + sum(customCostTypeValues[].cost)
    Per cost code qty:    quantity field (cumulative to-date production qty)
    Job total:            totalCost field (matches HCSS Job Summary report)

    NOTE: POST /v1/jobCostsToDate/search can over-count on some jobs (e.g. 11001).
    NOTE: POST /v1/costCode/progress/advancedRequest without date filter can hang on
    large admin jobs (e.g. 11001). jobs/{{id}}/costs quantity matches progress
    MAX(toDateQuantity) — verified on job 25006 (45/45 jobs).
Variance     ->  Expected All Cost - Actual Cost  (positive = under budget)

Verified against HCSS portal job 25006: Expected All Cost = $1,293,979.15

Usage:
    python scripts/get_job_expected_all_cost.py --job-code 25006
    python scripts/get_job_expected_all_cost.py --job-id <UUID> --verbose --out result.json
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
_COST_CODES_V1 = f"{_BASE}/v1/costCodes"
_PROGRESS      = f"{_BASE}/v1/costCode/progress/advancedRequest"
_JOB_COSTS     = f"{_BASE}/v1/jobs"  # GET {{base}}/{{jobId}}/costs

_BUDGET_FIELDS = [
    "laborDollars",
    "equipmentDollars",
    "materialDollars",
    "subcontractDollars",
    "supplyDollars",
]


def _sf(val: Any) -> float:
    try:
        return float(val or 0)
    except (TypeError, ValueError):
        return 0.0


def _with_retry(fn, *, label: str = "", max_retries: int = 8) -> Any:
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


def _post_pages(
    client: HCSSClient,
    url: str,
    payload: dict,
    *,
    label: str,
    delay: float = 0.3,
) -> list[dict]:
    rows, cursor = [], None
    while True:
        req = dict(payload)
        if cursor:
            req["cursor"] = cursor
        raw = _with_retry(lambda r=req: client._request("POST", url, json=r), label=label)
        batch, next_cursor = client._extract_results_and_cursor(raw)
        rows.extend(batch)
        if not next_cursor:
            break
        cursor = next_cursor
        time.sleep(delay)
    return rows


def _get_pages(
    client: HCSSClient,
    url: str,
    params: dict,
    *,
    label: str,
    delay: float = 0.5,
) -> list[dict]:
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


def _budgeted_all_cost(cc: dict[str, Any]) -> float:
    total = sum(_sf(cc.get(f)) for f in _BUDGET_FIELDS)
    custom = cc.get("customCostTypeDollars") or []
    if isinstance(custom, list):
        total += sum(
            _sf(item.get("dollars") or item.get("amount"))
            for item in custom
            if isinstance(item, dict)
        )
    elif isinstance(custom, (int, float)):
        total += _sf(custom)
    return total


def resolve_codes(client: HCSSClient, codes: list[str]) -> dict[str, str]:
    print(f"\n[resolve] Looking up {len(codes)} job code(s): {', '.join(codes)}")
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
        if str(c).strip() not in out:
            print(f"  WARNING: job code '{c}' not found in /v1/jobs")
    print(f"  Resolved {len(out)}/{len(codes)} job code(s)")
    return out


def fetch_meta(client: HCSSClient, job_ids: list[str]) -> dict[str, dict[str, str]]:
    try:
        raw = _with_retry(
            lambda: client._request("POST", _JOBS_ADV, json={"jobIds": job_ids}),
            label="jobs/advanced",
        )
        lst = raw if isinstance(raw, list) else raw.get("results", [])
    except Exception as exc:
        logger.warning("jobs/advanced failed: %s", exc)
        return {}
    return {
        (j.get("id") or ""): {
            "code": str(j.get("code") or j.get("jobCode") or ""),
            "name": str(j.get("description") or j.get("name") or ""),
        }
        for j in lst
        if j.get("id")
    }


def fetch_cost_codes(client: HCSSClient, job_id: str, label: str) -> list[dict[str, Any]]:
    print(f"  [budget]   Fetching costCodes v1 for {label} ...")
    rows = _get_pages(client, _COST_CODES_V1, {"jobId": job_id, "limit": 500}, label=f"costCodes {label}")
    print(f"  [budget]   {len(rows)} cost code(s)")
    return rows


def fetch_progress_qty_map(client: HCSSClient, job_id: str, label: str) -> dict[str, float]:
    """
    Legacy: POST /v1/costCode/progress/advancedRequest (no date filter).
    Prefer fetch_qty_map_from_job_costs — same values, one fast GET call.
    """
    print(f"  [progress] Fetching progress for {label} ...")
    rows = _post_pages(client, _PROGRESS, {"jobId": job_id, "limit": 500}, label=f"progress {label}")
    print(f"  [progress] {len(rows)} record(s)")

    qty_map: dict[str, float] = {}
    for p in rows:
        ccid = p.get("costCodeId") or ""
        if not ccid:
            continue
        qty = _sf(p.get("toDateQuantity") or p.get("quantityToDate"))
        qty_map[ccid] = max(qty_map.get(ccid, 0.0), qty)
    return qty_map


def _parse_job_costs_response(raw: dict[str, Any]) -> tuple[dict[str, float], dict[str, float], float]:
    """Parse GET /jobs/{id}/costs into (actual_map, qty_map, totalCost)."""
    cc_rows = raw.get("costCodeCosts") or []
    total_cost = round(_sf(raw.get("totalCost")), 2)
    actual_map: dict[str, float] = {}
    qty_map: dict[str, float] = {}
    for row in cc_rows:
        ccid = row.get("costCodeId") or ""
        if not ccid:
            continue
        actual_map[ccid] = round(_actual_from_job_cost_row(row), 2)
        qty_map[ccid] = _sf(row.get("quantity"))
    return actual_map, qty_map, total_cost


def fetch_qty_map_from_job_costs(client: HCSSClient, job_id: str, label: str) -> dict[str, float]:
    """Cumulative production qty per cost code from GET /jobs/{id}/costs."""
    print(f"  [qty]      Fetching jobs/{label}/costs (quantity) ...")
    raw = _with_retry(
        lambda: client._request("GET", f"{_JOB_COSTS}/{job_id}/costs"),
        label=f"jobs/{label}/costs qty",
    )
    _, qty_map, _ = _parse_job_costs_response(raw)
    with_qty = sum(1 for q in qty_map.values() if q > 0)
    print(f"  [qty]      {with_qty} cost code(s) with production qty")
    return qty_map


def _actual_from_job_cost_row(row: dict[str, Any]) -> float:
    """Sum all cost-type dollars on a GET /jobs/{{id}}/costs costCodeCosts row."""
    base = (
        _sf(row.get("laborCost"))
        + _sf(row.get("equipmentCost"))
        + _sf(row.get("materialCost"))
        + _sf(row.get("subcontractCost"))
        + _sf(row.get("truckingCost"))
    )
    custom = sum(
        _sf(item.get("cost"))
        for item in (row.get("customCostTypeValues") or [])
        if isinstance(item, dict)
    )
    return base + custom


def fetch_actual_costs(
    client: HCSSClient,
    job_id: str,
    label: str,
) -> tuple[dict[str, float], float]:
    """
    GET /v1/jobs/{jobId}/costs

    Authoritative actual-cost source — matches HCSS Job Summary / Cost Report.
    Includes custom cost types via customCostTypeValues (missing from jobCostsToDate
    on some jobs, e.g. 25048 +$1,180; jobCostsToDate over-counts others, e.g. 11001).

    Returns (actual_map, totalCost).
    """
    print(f"  [actual]   Fetching jobs/{label}/costs ...")
    raw = _with_retry(
        lambda: client._request("GET", f"{_JOB_COSTS}/{job_id}/costs"),
        label=f"jobs/{label}/costs",
    )
    actual_map, _, total_cost = _parse_job_costs_response(raw)
    cc_sum = round(sum(actual_map.values()), 2)
    if abs(cc_sum - total_cost) > 0.05 and total_cost > 0:
        logger.warning(
            "jobs/%s/costs: totalCost=%.2f != sum(costCodeCosts)=%.2f",
            label, total_cost, cc_sum,
        )
    print(f"  [actual]   {len(actual_map)} cost code(s), total ${total_cost:,.2f}")
    return actual_map, total_cost


def fetch_job_costs(
    client: HCSSClient,
    job_id: str,
    label: str,
) -> tuple[dict[str, float], dict[str, float], float]:
    """Single GET /jobs/{id}/costs — returns (actual_map, qty_map, totalCost)."""
    print(f"  [costs]    Fetching jobs/{label}/costs ...")
    raw = _with_retry(
        lambda: client._request("GET", f"{_JOB_COSTS}/{job_id}/costs"),
        label=f"jobs/{label}/costs",
    )
    actual_map, qty_map, total_cost = _parse_job_costs_response(raw)
    with_qty = sum(1 for q in qty_map.values() if q > 0)
    print(
        f"  [costs]    {len(actual_map)} cost code(s), "
        f"{with_qty} with qty, total ${total_cost:,.2f}"
    )
    return actual_map, qty_map, total_cost


def _compute_expected_for_cc(
    *,
    is_tm: bool,
    is_cap_expected: bool,
    budgeted_qty: float,
    budgeted_cost: float,
    actual_qty: float,
    actual_cost: float,
) -> tuple[float, str, float | None]:
    """
    Returns (expected_all_cost, calc_method, raw_formula_or_none).

    Matches HCSS portal when isCapExpected is true (default on all cost codes):
        expected = min((actualQty/budgetQty)*budget, budget)
    """
    if is_tm:
        return actual_cost, "T&M (expected = actual)", None

    if budgeted_qty > 0 and budgeted_cost > 0:
        formula = budgeted_cost * (actual_qty / budgeted_qty)
        if is_cap_expected:
            capped = min(formula, budgeted_cost)
            if capped < formula - 0.005:
                return capped, "cap-at-budget (qty > 100%)", formula
            return capped, "formula", formula
        return formula, "formula (no cap)", formula

    return 0.0, "zero (no qty/budget)", None


def compute_expected_all_cost(
    cost_codes: list[dict[str, Any]],
    qty_map: dict[str, float],
    actual_map: dict[str, float],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    total_expected = 0.0
    total_actual = 0.0
    total_budgeted = 0.0
    seen_ccids: set[str] = set()

    for cc in cost_codes:
        ccid = cc.get("id") or ""
        if not ccid:
            continue
        seen_ccids.add(ccid)

        is_tm = bool(cc.get("isTm"))
        is_cap = bool(cc.get("isCapExpected", True))
        bq = _sf(cc.get("quantity"))
        bc = _budgeted_all_cost(cc)
        aq = qty_map.get(ccid, 0.0)
        ac = actual_map.get(ccid, 0.0)

        total_budgeted += bc
        total_actual += ac

        expected, method, raw_formula = _compute_expected_for_cc(
            is_tm=is_tm,
            is_cap_expected=is_cap,
            budgeted_qty=bq,
            budgeted_cost=bc,
            actual_qty=aq,
            actual_cost=ac,
        )

        variance = expected - ac
        total_expected += expected

        rows.append({
            "costCodeId": ccid,
            "costCode": cc.get("code") or "",
            "description": cc.get("description") or "",
            "isTm": is_tm,
            "isCapExpected": is_cap,
            "budgetedQty": round(bq, 4),
            "actualQty": round(aq, 4),
            "completionPct": round(aq / bq * 100, 2) if bq > 0 else None,
            "budgetedAllCost": round(bc, 2),
            "rawFormula": round(raw_formula, 2) if raw_formula is not None else None,
            "expectedAllCost": round(expected, 2),
            "actualCost": round(ac, 2),
            "variance": round(variance, 2),
            "calcMethod": method,
        })

    for ccid, ac in actual_map.items():
        if ccid in seen_ccids or ac == 0:
            continue
        total_actual += ac
        rows.append({
            "costCodeId": ccid,
            "costCode": "",
            "description": "(unbudgeted / unknown cost code)",
            "isTm": False,
            "isCapExpected": False,
            "budgetedQty": 0.0,
            "actualQty": round(qty_map.get(ccid, 0.0), 4),
            "completionPct": None,
            "budgetedAllCost": 0.0,
            "rawFormula": None,
            "expectedAllCost": 0.0,
            "actualCost": round(ac, 2),
            "variance": round(-ac, 2),
            "calcMethod": "unbudgeted",
        })

    rows.sort(key=lambda r: r["costCode"] or r["costCodeId"])

    totals = {
        "budgetedAllCost": round(total_budgeted, 2),
        "expectedAllCost": round(total_expected, 2),
        "actualCost": round(total_actual, 2),
        "variance": round(total_expected - total_actual, 2),
        "utilizationPct": (
            round(total_actual / total_expected * 100, 2) if total_expected > 0 else None
        ),
    }
    return rows, totals


def process_job(client: HCSSClient, job_id: str, job_code: str, job_name: str) -> dict[str, Any]:
    label = job_code or job_id[:8]
    sep = "-" * 64
    print(f"\n{sep}")
    print(f"  Job: {job_code}  |  {job_name}")
    print(f"  ID:  {job_id}")
    print(sep)

    cost_codes = fetch_cost_codes(client, job_id, label)
    time.sleep(0.5)
    actual_map, qty_map, job_total_cost = fetch_job_costs(client, job_id, label)

    cc_rows, totals = compute_expected_all_cost(cost_codes, qty_map, actual_map)
    totals["actualCost"] = job_total_cost
    totals["variance"] = round(totals["expectedAllCost"] - job_total_cost, 2)
    if totals["expectedAllCost"] > 0:
        totals["utilizationPct"] = round(job_total_cost / totals["expectedAllCost"] * 100, 2)
    else:
        totals["utilizationPct"] = None

    w = 58
    exp_s = "$" + f"{totals['expectedAllCost']:,.2f}"
    act_s = "$" + f"{totals['actualCost']:,.2f}"
    var_s = "$" + f"{totals['variance']:,.2f}"
    bud_s = "$" + f"{totals['budgetedAllCost']:,.2f}"
    print(f"\n  +{'-'*w}+")
    print(f"  | {'EXPECTED ALL COST SUMMARY':^{w}} |")
    print(f"  | {label:^{w}} |")
    print(f"  +{'-'*w}+")
    print(f"  | {'Expected All Cost:':<26} {exp_s:>29} |")
    print(f"  | {'Actual Cost:':<26} {act_s:>29} |")
    print(f"  | {'Variance (expected-actual):':<26} {var_s:>29} |")
    if totals["utilizationPct"] is not None:
        util_s = f"{totals['utilizationPct']:.1f}%"
        print(f"  | {'Utilization %:':<26} {util_s:>29} |")
    print(f"  +{'-'*w}+")
    print(f"  | {'Budgeted All Cost (info):':<26} {bud_s:>29} |")
    print(f"  | {'Cost codes:':<26} {len(cc_rows):>29,} |")
    print(f"  +{'-'*w}+")

    capped = [r for r in cc_rows if r["calcMethod"] == "cap-at-budget (qty > 100%)"]
    if capped:
        cap_total = sum(r["rawFormula"] - r["expectedAllCost"] for r in capped if r["rawFormula"])
        print(f"\n  Note: {len(capped)} cost code(s) capped at budget (isCapExpected), "
              f"reducing expected by ${cap_total:,.2f} vs uncapped formula.")

    return {
        "jobId": job_id,
        "jobCode": job_code,
        "jobName": job_name,
        "totals": totals,
        "costCodes": cc_rows,
        "cappedCostCodeCount": len(capped),
    }


def print_verbose(result: dict[str, Any]) -> None:
    rows = [r for r in result.get("costCodes", []) if r["expectedAllCost"] or r["actualCost"]]
    if not rows:
        return
    print(f"\n  Cost Code Detail — {result.get('jobCode', '')}")
    hdr = (
        f"  {'Cost Code':<14} {'Description':<28} {'Expected':>14} "
        f"{'Actual':>14} {'Variance':>14} {'Compl%':>7}"
    )
    print(hdr)
    print("  " + "-" * (len(hdr) - 2))
    for r in rows:
        pct = f"{r['completionPct']:.1f}%" if r["completionPct"] is not None else "N/A"
        print(
            f"  {r['costCode']:<14} "
            f"{r['description'][:27]:<28} "
            f"${r['expectedAllCost']:>13,.2f} "
            f"${r['actualCost']:>13,.2f} "
            f"${r['variance']:>13,.2f} "
            f"{pct:>7}"
        )
    t = result["totals"]
    print("  " + "-" * (len(hdr) - 2))
    print(
        f"  {'TOTAL':<14} {'':<28} "
        f"${t['expectedAllCost']:>13,.2f} "
        f"${t['actualCost']:>13,.2f} "
        f"${t['variance']:>13,.2f}"
    )


def run(args: argparse.Namespace) -> list[dict[str, Any]]:
    client = HCSSClient()

    id_to_code: dict[str, str] = {}
    id_to_name: dict[str, str] = {}
    job_ids_raw: list[str] = list(args.job_id or [])

    if args.job_code:
        code_map = resolve_codes(client, list(args.job_code))
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
        meta = fetch_meta(client, need_meta)
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
        ge = sum(r["totals"]["expectedAllCost"] for r in ok)
        ga = sum(r["totals"]["actualCost"] for r in ok)
        gv = round(ge - ga, 2)
        print(f"\n  {'='*52}")
        print(f"  GRAND TOTAL  ({len(ok)} jobs)")
        print(f"  {'-'*52}")
        print(f"  Expected All Cost  :  ${ge:>27,.2f}")
        print(f"  Actual Cost        :  ${ga:>27,.2f}")
        print(f"  Variance           :  ${gv:>27,.2f}")
        print(f"  {'='*52}")

    return results


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Get Expected All Cost, Actual Cost, and Variance from HCSS.",
    )
    parser.add_argument("--job-id", nargs="+", metavar="UUID", help="One or more HCSS job UUIDs")
    parser.add_argument("--job-code", nargs="+", metavar="CODE", help="One or more job codes, e.g. 25006")
    parser.add_argument("--out", default=None, metavar="FILE", help="Save full JSON results to this file")
    parser.add_argument("--summary-only", action="store_true", help="Output only expectedAllCost and actualCost per job")
    parser.add_argument("--verbose", "-v", action="store_true", help="Print per-cost-code breakdown table")
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    args = parser.parse_args()

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    if not args.job_id and not args.job_code:
        parser.print_help()
        sys.exit(1)

    print("\n" + "=" * 64)
    print("  HCSS Expected All Cost vs Actual Cost")
    print("=" * 64)

    results = run(args)

    if args.out:
        out_path = Path(args.out)
        if args.summary_only:
            payload = [
                {
                    "jobCode": r.get("jobCode"),
                    "expectedAllCost": r.get("totals", {}).get("expectedAllCost"),
                    "actualCost": r.get("totals", {}).get("actualCost"),
                    "error": r.get("error"),
                }
                for r in results
            ]
        else:
            payload = results
        out_path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
        print(f"\n  Results saved -> {out_path.resolve()}")
    elif args.summary_only:
        summary = [
            {
                "jobCode": r.get("jobCode"),
                "expectedAllCost": r.get("totals", {}).get("expectedAllCost"),
                "actualCost": r.get("totals", {}).get("actualCost"),
                "error": r.get("error"),
            }
            for r in results
        ]
        print("\n  JSON Summary (expected + actual only):")
        print(json.dumps(summary, indent=2, default=str))
    else:
        summary = [
            {
                "jobId": r.get("jobId"),
                "jobCode": r.get("jobCode"),
                "jobName": r.get("jobName"),
                "totals": r.get("totals"),
                "cappedCostCodeCount": r.get("cappedCostCodeCount"),
                "error": r.get("error"),
            }
            for r in results
        ]
        print("\n  JSON Summary:")
        print(json.dumps(summary, indent=2, default=str))

    print("\n  Done.\n")


if __name__ == "__main__":
    main()
