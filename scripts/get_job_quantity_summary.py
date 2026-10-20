"""
get_job_quantity_summary.py
════════════════════════════
Returns total Budgeted Quantity and Actual Quantity (summed across all cost
codes) for one or more HCSS HeavyJob projects.

Data sources (confirmed from test_hcss_quantity_debug.py / test_actual_budget_quantity.py):

  BUDGETED QUANTITY → POST /v2/costCodes/advancedRequest
      Field: "quantity"  (same field that HCSS portal shows as "Budgeted Quantity")
      Only cost codes with quantity > 0 are counted.
      Unit of measure is preserved per cost code for the detail table.

  ACTUAL QUANTITY   → POST /v1/costCode/progress/advancedRequest  (NO date filter)
      Field: SUM("quantity") per cost_code_id across ALL progress records.
      No date filter = full project lifetime (cumulative).
      Do NOT use quantityToDate — it can reflect a single snapshot, not the sum.

Usage:
    python scripts/get_job_quantity_summary.py --job-code 25006
    python scripts/get_job_quantity_summary.py --job-code 25001 25006 26004
    python scripts/get_job_quantity_summary.py --job-id <UUID>
    python scripts/get_job_quantity_summary.py --job-code 25006 --verbose
    python scripts/get_job_quantity_summary.py --job-code 25006 --out results.json
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path
from typing import Any

_RATE_LIMIT_COOLDOWN = 3.0   # seconds to pause between major API call groups

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from dotenv import load_dotenv
load_dotenv(_REPO_ROOT / ".env")

from payroll_verification.hcss_client import HCSSClient  # noqa: E402

logging.basicConfig(level=logging.WARNING, format="%(levelname)s  %(name)s  %(message)s")
logger = logging.getLogger(__name__)

_BASE          = "https://api.hcssapps.com/heavyjob/api"
_COST_CODES_V2 = f"{_BASE}/v2/costCodes/advancedRequest"
_PROGRESS_V1   = f"{_BASE}/v1/costCode/progress/advancedRequest"
_JOBS_LIST     = f"{_BASE}/v1/jobs"
_JOBS_ADVANCED = f"{_BASE}/v1/jobs/advanced"


# ── Helpers ───────────────────────────────────────────────────────────────────

def _safe_float(val: Any) -> float:
    try:
        return float(val or 0)
    except (TypeError, ValueError):
        return 0.0


def _with_retry(fn, *, label: str = "", max_retries: int = 8) -> Any:
    """Call fn(), retrying on 429 with exponential back-off."""
    import requests as _requests
    retries = 0
    while True:
        try:
            return fn()
        except _requests.exceptions.HTTPError as exc:
            code = exc.response.status_code if exc.response is not None else None
            if code == 429 and retries < max_retries:
                retry_after = None
                if exc.response is not None:
                    retry_after = exc.response.headers.get("Retry-After")
                wait = float(retry_after) if retry_after else min(120.0, (2 ** retries) * 5.0)
                retries += 1
                print(f"  [429] rate-limited on {label}. Waiting {wait:.0f}s (retry {retries}/{max_retries}) ...")
                time.sleep(wait)
                continue
            raise


def _paginate_post(client: HCSSClient, url: str, payload: dict, *, label: str = "", delay: float = 1.0) -> list[dict]:
    """Fetch all cursor-paginated pages from a POST endpoint with 429 retry."""
    all_rows: list[dict] = []
    cursor: str | None = None
    page = 0

    while True:
        req = dict(payload)
        if cursor:
            req["cursor"] = cursor

        raw = _with_retry(
            lambda r=req: client._request("POST", url, json=r),
            label=label,
        )
        results, next_cursor = client._extract_results_and_cursor(raw)

        page += 1
        all_rows.extend(results)
        logger.debug("  [%s] page %d — %d rows (total %d)", label, page, len(results), len(all_rows))

        if not next_cursor:
            break
        cursor = next_cursor
        time.sleep(delay)

    return all_rows


def _paginate_get(client: HCSSClient, url: str, params: dict, *, label: str = "", delay: float = 0.5) -> list[dict]:
    """Fetch all cursor-paginated pages from a GET endpoint with 429 retry."""
    all_rows: list[dict] = []
    cursor: str | None = None

    while True:
        p = dict(params)
        if cursor:
            p["cursor"] = cursor

        raw = _with_retry(
            lambda pr=p: client._request("GET", url, params=pr),
            label=label,
        )
        results, next_cursor = client._extract_results_and_cursor(raw)
        all_rows.extend(results)

        if not next_cursor:
            break
        cursor = next_cursor
        time.sleep(delay)

    return all_rows


# ── Job resolution ────────────────────────────────────────────────────────────

def resolve_job_codes(client: HCSSClient, job_codes: list[str]) -> dict[str, str]:
    """Return {job_code: uuid} by scanning GET /v1/jobs."""
    if not job_codes:
        return {}
    print(f"\n[resolve] Looking up {len(job_codes)} job code(s): {', '.join(job_codes)}")
    rows = _paginate_get(client, _JOBS_LIST, {"limit": 500}, label="jobs list")
    target = {str(c).strip() for c in job_codes}
    code_to_uuid: dict[str, str] = {}
    for job in rows:
        code = str(
            job.get("code") or job.get("jobCode") or job.get("number") or ""
        ).strip()
        if code in target:
            uid = job.get("id") or job.get("jobId") or ""
            if uid:
                code_to_uuid[code] = uid
    for code in job_codes:
        if str(code).strip() not in code_to_uuid:
            print(f"  WARNING: job code '{code}' not found in /v1/jobs")
    print(f"  Resolved {len(code_to_uuid)}/{len(job_codes)} job code(s)")
    return code_to_uuid


def fetch_job_metadata(client: HCSSClient, job_ids: list[str]) -> dict[str, dict]:
    """Return {uuid: {code, name}} via POST /v1/jobs/advanced."""
    if not job_ids:
        return {}
    try:
        raw = _with_retry(
            lambda: client._request("POST", _JOBS_ADVANCED, json={"jobIds": job_ids}),
            label="jobs/advanced",
        )
        job_list = raw if isinstance(raw, list) else raw.get("results", [])
    except Exception as exc:
        logger.warning("Could not fetch job metadata: %s", exc)
        return {}
    meta: dict[str, dict] = {}
    for job in job_list:
        uid = job.get("id") or ""
        if uid:
            meta[uid] = {
                "code": str(job.get("code") or job.get("jobCode") or ""),
                "name": str(job.get("description") or job.get("name") or ""),
            }
    return meta


# ── Step 1: Budgeted quantity ─────────────────────────────────────────────────

def fetch_budgeted_quantity(client: HCSSClient, job_id: str, label: str) -> tuple[float, list[dict]]:
    """
    POST /v2/costCodes/advancedRequest

    Planned / budgeted quantity is the "quantity" field on each cost code.
    Only cost codes with quantity > 0 are counted.

    Returns:
        (total_budgeted_qty, list_of_cost_code_dicts)
    """
    print(f"  [budget qty] Fetching cost codes for {label} ...")
    rows = _paginate_post(
        client, _COST_CODES_V2,
        {"jobIds": [job_id], "limit": 500},
        label="cost codes",
    )
    print(f"  [budget qty] {len(rows)} cost code(s) returned")

    cc_list: list[dict] = []
    total_budgeted = 0.0

    for cc in rows:
        planned = _safe_float(
            cc.get("quantity")
            or cc.get("plannedQuantity")
            or cc.get("budgetQuantity")
            or cc.get("budgetedQuantity")
        )
        if planned <= 0:
            continue  # skip cost codes with no quantity budget

        cc_list.append({
            "cost_code_id":  cc.get("id") or "",
            "cost_code":     cc.get("code") or cc.get("costCode") or "",
            "description":   cc.get("description") or cc.get("name") or "",
            "unit":          cc.get("unit") or cc.get("unitOfMeasure") or cc.get("uom") or "UNIT",
            "budgeted_qty":  round(planned, 4),
        })
        total_budgeted += planned

    print(f"  [budget qty] {len(cc_list)} cost codes with quantity > 0  |  total budgeted = {total_budgeted:,.4f}")
    return round(total_budgeted, 4), cc_list


# ── Step 2: Actual (installed) quantity ───────────────────────────────────────

def fetch_actual_quantity(client: HCSSClient, job_id: str, label: str) -> tuple[float, dict[str, float]]:
    """
    POST /v1/costCode/progress/advancedRequest  — NO date filter (full lifetime).

    Actual quantity = SUM(progress["quantity"]) per cost_code_id.

    Using "quantity" (period production) summed over all records gives the same
    cumulative total as the HCSS portal "Actual Quantity" column.  Do not use
    quantityToDate directly — it is a snapshot on each record and summing it
    causes double-counting.

    Returns:
        (total_actual_qty, {cost_code_id: actual_qty})
    """
    print(f"  [actual qty] Fetching progress for {label} (no date filter = full lifetime) ...")
    rows = _paginate_post(
        client, _PROGRESS_V1,
        {"jobId": job_id, "limit": 500},
        label="progress",
        delay=1.0,
    )
    print(f"  [actual qty] {len(rows)} progress record(s) returned")

    installed_map: dict[str, float] = {}  # cost_code_id → cumulative qty

    for p in rows:
        ccid = p.get("costCodeId") or p.get("id") or ""
        if not ccid:
            continue
        qty = _safe_float(p.get("quantity"))
        installed_map[ccid] = installed_map.get(ccid, 0.0) + qty

    total_actual = sum(installed_map.values())
    print(f"  [actual qty] {len(installed_map)} cost code(s) with progress  |  total actual = {total_actual:,.4f}")
    return round(total_actual, 4), installed_map


# ── Step 3: Join & build cost-code rows ──────────────────────────────────────

def build_cc_rows(cc_list: list[dict], installed_map: dict[str, float]) -> list[dict]:
    rows: list[dict] = []
    for cc in cc_list:
        ccid    = cc["cost_code_id"]
        planned = cc["budgeted_qty"]
        actual  = round(installed_map.get(ccid, 0.0), 4)
        pct     = round((actual / planned) * 100, 2) if planned > 0 else None
        remaining = round(max(0.0, planned - actual), 4)

        if pct is None:
            status = "ON_TRACK"
        elif pct >= 100.0:
            status = "OVER_RISK"
        elif pct >= 75.0:
            status = "NEAR_COMPLETION"
        else:
            status = "ON_TRACK"

        rows.append({
            "cost_code_id":   ccid,
            "cost_code":      cc["cost_code"],
            "description":    cc["description"],
            "unit":           cc["unit"],
            "budgeted_qty":   planned,
            "actual_qty":     actual,
            "remaining_qty":  remaining,
            "percent_complete": pct,
            "status":         status,
        })

    rows.sort(key=lambda r: r["cost_code"] or r["cost_code_id"])
    return rows


# ── Process one job ───────────────────────────────────────────────────────────

def process_job(client: HCSSClient, job_id: str, job_code: str, job_name: str) -> dict:
    label = job_code or job_id[:8]
    sep = "-" * 64
    print(f"\n{sep}")
    print(f"  Job: {job_code}  |  {job_name}")
    print(f"  ID:  {job_id}")
    print(sep)

    total_budgeted, cc_list   = fetch_budgeted_quantity(client, job_id, label)
    total_actual,   inst_map  = fetch_actual_quantity(client, job_id, label)
    cc_rows                   = build_cc_rows(cc_list, inst_map)

    # Only count units if all CCs use the same UOM (otherwise label as "mixed")
    units = {r["unit"] for r in cc_rows if r["unit"]}
    unit_label = next(iter(units)) if len(units) == 1 else "mixed units"

    pct_complete = round((total_actual / total_budgeted) * 100, 2) if total_budgeted > 0 else None
    remaining    = round(max(0.0, total_budgeted - total_actual), 4)

    # ── Console summary box ───────────────────────────────────────────────
    w = 60
    print(f"\n  +{'-'*w}+")
    print(f"  | QUANTITY SUMMARY: {label:<{w-19}}|")
    print(f"  +{'-'*w}+")
    print(f"  | {'Total Budgeted Quantity:':<30} {total_budgeted:>22,.4f} {unit_label} |" if len(unit_label) <= 8 else f"  | {'Total Budgeted Quantity:':<30} {total_budgeted:>22,.4f} |")
    print(f"  | {'Total Actual Quantity:':<30} {total_actual:>22,.4f} |")
    print(f"  | {'Remaining Quantity:':<30} {remaining:>22,.4f} |")
    if pct_complete is not None:
        print(f"  | {'Percent Complete:':<30} {pct_complete:>21.2f}% |")
    print(f"  | {'Cost Codes with Qty Budget:':<30} {len(cc_rows):>29,} |")
    print(f"  | {'Unit(s):':<30} {unit_label:>29} |")
    print(f"  +{'-'*w}+")
    print(f"  | Source — Budgeted: /v2/costCodes  field: quantity          |")
    print(f"  | Source — Actual:   /v1/costCode/progress  SUM(quantity)    |")
    print(f"  +{'-'*w}+")

    return {
        "job_id":               job_id,
        "job_code":             job_code,
        "job_name":             job_name,
        "total_budgeted_qty":   total_budgeted,
        "total_actual_qty":     total_actual,
        "remaining_qty":        remaining,
        "percent_complete":     pct_complete,
        "cost_code_count":      len(cc_rows),
        "unit_label":           unit_label,
        "sources": {
            "budgeted": "POST /v2/costCodes/advancedRequest — field: quantity",
            "actual":   "POST /v1/costCode/progress/advancedRequest (no date filter) — SUM(quantity)",
        },
        "cost_codes": cc_rows,
    }


# ── Verbose detail table ──────────────────────────────────────────────────────

def print_verbose(result: dict) -> None:
    rows = result.get("cost_codes", [])
    if not rows:
        return

    print(f"\n  Cost Code Detail — {result.get('job_code', '')}")
    print(f"  {'Cost Code':<16} {'Description':<34} {'Unit':<6} {'Budgeted':>12} {'Actual':>12} {'Remaining':>12} {'%':>7}  Status")
    print("  " + "─" * 108)

    for r in rows:
        pct = f"{r['percent_complete']:.2f}%" if r["percent_complete"] is not None else "N/A"
        status_sym = {"OVER_RISK": "⚠", "NEAR_COMPLETION": "~", "ON_TRACK": "✓"}.get(r["status"], "")
        print(
            f"  {r['cost_code']:<16}"
            f" {r['description'][:33]:<34}"
            f" {r['unit']:<6}"
            f" {r['budgeted_qty']:>12,.4f}"
            f" {r['actual_qty']:>12,.4f}"
            f" {r['remaining_qty']:>12,.4f}"
            f" {pct:>7}"
            f"  {status_sym} {r['status']}"
        )

    print("  " + "─" * 108)
    pct_str = f"{result['percent_complete']:.2f}%" if result["percent_complete"] is not None else "N/A"
    print(
        f"  {'TOTAL':<16} {'':<34} {'':<6}"
        f" {result['total_budgeted_qty']:>12,.4f}"
        f" {result['total_actual_qty']:>12,.4f}"
        f" {result['remaining_qty']:>12,.4f}"
        f" {pct_str:>7}"
    )


# ── Orchestration ─────────────────────────────────────────────────────────────

def run(args: argparse.Namespace) -> list[dict]:
    client = HCSSClient()

    job_ids_raw: list[str] = list(args.job_id or [])
    id_to_code:  dict[str, str] = {}
    id_to_name:  dict[str, str] = {}

    # Resolve job codes → UUIDs
    if args.job_code:
        code_map = resolve_job_codes(client, list(args.job_code))
        for code, uid in code_map.items():
            job_ids_raw.append(uid)
            id_to_code[uid] = code
        time.sleep(_RATE_LIMIT_COOLDOWN)  # breathe after the jobs list call

    # Deduplicate
    seen: set[str] = set()
    job_ids: list[str] = []
    for jid in (j.strip() for j in job_ids_raw):
        if jid and jid not in seen:
            seen.add(jid)
            job_ids.append(jid)

    if not job_ids:
        print("\nERROR: No job IDs found. Provide --job-id or --job-code.")
        sys.exit(1)

    # Fetch metadata for any IDs that came in without a code/name
    need_meta = [j for j in job_ids if j not in id_to_name]
    if need_meta:
        meta = fetch_job_metadata(client, need_meta)
        for uid, m in meta.items():
            id_to_code.setdefault(uid, m["code"])
            id_to_name[uid] = m["name"]
        time.sleep(_RATE_LIMIT_COOLDOWN)  # breathe after jobs/advanced call

    results: list[dict] = []
    for idx, jid in enumerate(job_ids, 1):
        code = id_to_code.get(jid, "")
        name = id_to_name.get(jid, "")
        try:
            result = process_job(client, jid, code, name)
        except Exception as exc:
            print(f"\n  ERROR processing {code or jid}: {exc}")
            logger.exception("Failed: %s", jid)
            result = {"job_id": jid, "job_code": code, "job_name": name, "error": str(exc)}
        results.append(result)

        if args.verbose and "error" not in result:
            print_verbose(result)

        if idx < len(job_ids):
            time.sleep(1.0)  # polite gap between jobs

    # Grand total for multi-job runs
    ok = [r for r in results if "error" not in r]
    if len(ok) > 1:
        gb = sum(r["total_budgeted_qty"] for r in ok)
        ga = sum(r["total_actual_qty"]   for r in ok)
        gr = round(max(0.0, gb - ga), 4)
        gp = round(ga / gb * 100, 2) if gb > 0 else None
        print(f"\n  {'='*54}")
        print(f"  GRAND TOTAL  ({len(ok)} jobs)")
        print(f"  {'-'*54}")
        print(f"  Total Budgeted Quantity : {gb:>25,.4f}")
        print(f"  Total Actual Quantity   : {ga:>25,.4f}")
        print(f"  Remaining               : {gr:>25,.4f}")
        if gp is not None:
            print(f"  Overall % Complete      : {gp:>24.2f}%")
        print(f"  {'='*54}")

    return results


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Get total Budgeted Quantity and Actual Quantity per job from HCSS HeavyJob.\n"
            "Sums across ALL cost codes that have a quantity budget.\n\n"
            "Sources:\n"
            "  Budgeted  → POST /v2/costCodes/advancedRequest (field: quantity)\n"
            "  Actual    → POST /v1/costCode/progress/advancedRequest SUM(quantity)"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--job-id", nargs="+", metavar="UUID",
        help="One or more HCSS job UUIDs",
    )
    parser.add_argument(
        "--job-code", nargs="+", metavar="CODE",
        help="One or more human job codes, e.g. 25006 26004",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="Print per-cost-code breakdown table",
    )
    parser.add_argument(
        "--out", default=None, metavar="FILE",
        help="Save full JSON results to this file",
    )
    parser.add_argument(
        "--debug", action="store_true",
        help="Enable debug logging",
    )
    args = parser.parse_args()

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    if not args.job_id and not args.job_code:
        parser.print_help()
        sys.exit(1)

    print("\n" + "=" * 64)
    print("  HCSS Job Quantity Summary — Budgeted vs Actual")
    print("=" * 64)

    results = run(args)

    if args.out:
        out_path = Path(args.out)
        out_path.write_text(
            json.dumps(results, indent=2, default=str),
            encoding="utf-8",
        )
        print(f"\n  Results saved → {out_path.resolve()}")
    else:
        summary = [
            {
                "job_id":             r.get("job_id"),
                "job_code":           r.get("job_code"),
                "job_name":           r.get("job_name"),
                "total_budgeted_qty": r.get("total_budgeted_qty"),
                "total_actual_qty":   r.get("total_actual_qty"),
                "remaining_qty":      r.get("remaining_qty"),
                "percent_complete":   r.get("percent_complete"),
                "cost_code_count":    r.get("cost_code_count"),
                "unit_label":         r.get("unit_label"),
            }
            for r in results
        ]
        print("\n  JSON Summary:")
        print(json.dumps(summary, indent=2, default=str))

    print("\n  Done.\n")


if __name__ == "__main__":
    main()
