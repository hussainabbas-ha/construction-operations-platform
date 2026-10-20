"""
verify_progress_quantity.py
────────────────────────────────────────────────────────────────────────────────
Fetch actual/installed quantity per cost code from HCSS using the
costCode/progress/advancedRequest endpoint — the same source as the
HCSS portal "Quantity" column.

Used to verify that this endpoint gives accurate installed quantities
before wiring it into the main budget module.

HOW IT WORKS:
  Step 1 — GET /v1/timeCardInfo (startDate, endDate)
            Discover active job IDs in the date window.

  Step 2 — GET /v1/jobs/advanced  (job IDs)
            Resolve job codes/names for display.

  Step 3 — GET /v1/costCodes  (per job)
            Get planned quantity and unit of measure per cost code.

  Step 4 — POST /v1/costCode/progress/advancedRequest  (per job, date range)
            Returns one row per cost code per day that had progress logged.
            Sum the "quantity" field per costCodeId for the full range total.
            This is the installed/actual quantity matching the HCSS portal.

USAGE:
  python scripts/verify_progress_quantity.py --date 2026-06-15
  python scripts/verify_progress_quantity.py --start 2026-06-01 --end 2026-06-15
  python scripts/verify_progress_quantity.py --start 2026-06-01 --end 2026-06-15 --job-code 25069
  python scripts/verify_progress_quantity.py --start 2026-06-01 --end 2026-06-15 --out progress.json

OPTIONS:
  --date       Single date YYYY-MM-DD
  --start      Range start YYYY-MM-DD
  --end        Range end YYYY-MM-DD
  --job-code   Filter to a specific job code (optional)
  --out        Save full JSON output to this file (optional)
  --verbose    Show raw API row counts and debug info
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

_BASE         = "https://api.hcssapps.com/heavyjob/api/v1"
_TIMECARDS    = f"{_BASE}/timeCardInfo"
_JOBS_ADV     = f"{_BASE}/jobs/advanced"
_COST_CODES   = f"{_BASE}/costCodes"
_PROGRESS     = f"{_BASE}/costCode/progress/advancedRequest"


# ── API helpers ───────────────────────────────────────────────────────────────

def paginate_get(client: HCSSClient, url: str, params: dict) -> list[dict]:
    """GET with cursor-based pagination."""
    rows: list[dict] = []
    cursor = None
    while True:
        if cursor:
            params = dict(params)
            params["cursor"] = cursor
        raw = client._request("GET", url, params=params)
        batch, next_cursor = client._extract_results_and_cursor(raw)
        rows.extend(batch)
        if not next_cursor:
            break
        cursor = next_cursor
    return rows


def paginate_post(client: HCSSClient, url: str, payload: dict) -> list[dict]:
    """POST with cursor-based pagination."""
    rows: list[dict] = []
    cursor = None
    while True:
        if cursor:
            payload = dict(payload)
            payload["cursor"] = cursor
        raw = client._request("POST", url, json=payload)
        batch, next_cursor = client._extract_results_and_cursor(raw)
        rows.extend(batch)
        if not next_cursor:
            break
        cursor = next_cursor
    return rows


# ── Step 1: active job IDs ────────────────────────────────────────────────────

def fetch_active_job_ids(client: HCSSClient, start: str, end: str, verbose: bool) -> list[str]:
    print(f"\n[Step 1] GET /v1/timeCardInfo  ({start} → {end})")
    rows = paginate_get(client, _TIMECARDS, {"startDate": start, "endDate": end})
    job_ids: dict[str, bool] = {}
    for tc in rows:
        jid = tc.get("jobId") or tc.get("job_id") or ""
        if jid:
            job_ids[jid] = True
    print(f"         {len(rows)} timecards → {len(job_ids)} active jobs")
    if verbose and rows:
        print(f"         Sample timecard keys: {list(rows[0].keys())}")
    return list(job_ids.keys())


# ── Step 2: resolve job codes ─────────────────────────────────────────────────

def fetch_job_map(client: HCSSClient, job_ids: list[str], verbose: bool) -> dict[str, dict]:
    """Returns { jobId: { jobCode, jobDescription } }"""
    print(f"\n[Step 2] POST /v1/jobs/advanced  ({len(job_ids)} jobs)")
    try:
        rows = paginate_post(client, _JOBS_ADV, {"jobIds": job_ids, "limit": 500})
    except Exception as exc:
        print(f"         WARNING: could not fetch job names: {exc}")
        return {}

    job_map: dict[str, dict] = {}
    for r in rows:
        jid = r.get("id") or r.get("jobId") or ""
        if jid:
            job_map[jid] = {
                "jobCode":        r.get("jobCode") or r.get("code") or "",
                "jobDescription": r.get("description") or r.get("jobDescription") or "",
            }
    print(f"         {len(job_map)} jobs resolved")
    return job_map


# ── Step 3: planned quantities from costCodes ─────────────────────────────────

def fetch_planned_quantities(
    client: HCSSClient,
    job_ids: list[str],
    verbose: bool,
) -> dict[str, dict]:
    """
    Returns { costCodeId: { plannedQty, unit, costCode, description, jobId } }
    Uses v1/costCodes — field "quantity" is the planned/budgeted quantity.
    """
    print(f"\n[Step 3] GET /v1/costCodes  ({len(job_ids)} jobs)")
    planned: dict[str, dict] = {}
    total_rows = 0
    for jid in job_ids:
        rows = paginate_get(client, _COST_CODES, {"jobId": jid, "limit": 500})
        total_rows += len(rows)
        for cc in rows:
            ccid = cc.get("id") or ""
            if not ccid:
                continue
            planned[ccid] = {
                "plannedQty":    float(cc.get("quantity") or 0),
                "unit":          cc.get("unitOfMeasure") or cc.get("unit") or "",
                "costCode":      cc.get("code") or "",
                "description":   cc.get("description") or "",
                "jobId":         jid,
            }
    print(f"         {total_rows} cost code rows → {len(planned)} unique cost codes")
    return planned


# ── Step 4: installed quantities from progress ────────────────────────────────

def fetch_installed_quantities(
    client: HCSSClient,
    job_ids: list[str],
    start: str,
    end: str,
    verbose: bool,
) -> dict[str, float]:
    """
    Returns { costCodeId: installed_quantity } using the progress endpoint.

    IMPORTANT — correct aggregation strategy:
    Each row has two quantity fields:
      - quantity        = quantity installed on that specific day (daily delta)
      - toDateQuantity  = cumulative installed quantity as of that day (running total)

    Summing 'quantity' across days gives an inflated result because each day's
    entry is already included in the next day's toDateQuantity.

    The correct value is: take the MAX toDateQuantity per cost code within the
    date range — this is the cumulative installed quantity as of the last
    active day in the range, matching what the HCSS portal shows.
    """
    print(f"\n[Step 4] POST /v1/costCode/progress/advancedRequest  ({start} → {end})")
    print(f"         {len(job_ids)} jobs — one API call per job")
    print(f"         Using MAX(toDateQuantity) per cost code (not SUM of daily quantity)")

    # { ccid: (latest_date_str, toDateQuantity) }
    latest: dict[str, tuple[str, float]] = {}
    total_rows = 0

    for i, jid in enumerate(job_ids, 1):
        try:
            rows = paginate_post(client, _PROGRESS, {
                "jobId":     jid,
                "startDate": f"{start}T00:00:00Z",
                "endDate":   f"{end}T23:59:59Z",
                "limit":     500,
            })
            total_rows += len(rows)

            for row in rows:
                ccid = row.get("costCodeId") or row.get("id") or ""
                if not ccid:
                    continue
                row_date   = row.get("date", "")
                to_date_qty = float(row.get("toDateQuantity") or 0)

                # Keep the entry with the latest date (highest toDateQuantity)
                if ccid not in latest or row_date > latest[ccid][0]:
                    latest[ccid] = (row_date, to_date_qty)

            if verbose:
                nonzero = sum(1 for _, (_, q) in latest.items() if q > 0)
                print(f"         job {i}/{len(job_ids)}: {jid[:8]}... → "
                      f"{len(rows)} rows  (running: {len(latest)} cost codes, "
                      f"{nonzero} with toDateQty > 0)")

            time.sleep(0.3)

        except Exception as exc:
            print(f"         WARNING: job {jid[:8]}... failed: {exc} — skipped")
            continue

    # Extract final toDateQuantity values
    installed = {ccid: qty for ccid, (_, qty) in latest.items()}

    nonzero_final = sum(1 for v in installed.values() if v > 0)
    print(f"         Total: {total_rows} progress rows → "
          f"{len(installed)} cost codes  ({nonzero_final} with installed qty > 0)")

    return installed


# ── Build results ─────────────────────────────────────────────────────────────

def build_results(
    installed: dict[str, float],
    planned: dict[str, dict],
    job_map: dict[str, dict],
    job_filter: str | None,
) -> list[dict]:
    results = []

    # Include all cost codes that had ANY installed quantity
    for ccid, inst_qty in installed.items():
        if inst_qty <= 0:
            continue

        cc_info = planned.get(ccid, {})
        jid = cc_info.get("jobId", "")
        job_info = job_map.get(jid, {})
        job_code = job_info.get("jobCode", "") or jid[:8]
        job_name = job_info.get("jobDescription", "")

        # Apply job filter
        if job_filter and job_code != job_filter:
            continue

        planned_qty = cc_info.get("plannedQty", 0.0)
        unit        = cc_info.get("unit", "")
        pct_complete = round((inst_qty / planned_qty) * 100, 2) if planned_qty > 0 else None

        results.append({
            "job_code":        job_code,
            "job_name":        job_name,
            "job_id":          jid,
            "cost_code_id":    ccid,
            "cost_code":       cc_info.get("costCode", ""),
            "description":     cc_info.get("description", ""),
            "unit":            unit,
            "installed_qty":   round(inst_qty, 4),
            "planned_qty":     round(planned_qty, 4),
            "pct_complete":    pct_complete,
        })

    # Sort by job_code then cost_code
    results.sort(key=lambda r: (r["job_code"], r["cost_code"]))
    return results


# ── Print table ───────────────────────────────────────────────────────────────

def print_table(results: list[dict], start: str, end: str) -> None:
    print(f"\n{'='*110}")
    print(f"  PROGRESS QUANTITY RESULTS  —  {start} → {end}")
    print(f"  Source: POST /v1/costCode/progress/advancedRequest")
    print(f"  Compare 'installed_qty' against HCSS portal 'Quantity' column with same date filter")
    print(f"{'='*110}")
    print(f"  {'Job':<8}  {'Code':<16}  {'Description':<32}  "
          f"{'Installed':>12}  {'Planned':>12}  {'%':>7}  {'Unit':<8}")
    print(f"  {'-'*8}  {'-'*16}  {'-'*32}  {'-'*12}  {'-'*12}  {'-'*7}  {'-'*8}")

    current_job = None
    for r in results:
        if r["job_code"] != current_job:
            if current_job is not None:
                print()
            current_job = r["job_code"]

        pct_str = f"{r['pct_complete']:.1f}%" if r["pct_complete"] is not None else "   N/A"
        desc = r["description"][:32]
        print(
            f"  {r['job_code']:<8}  {r['cost_code']:<16}  {desc:<32}  "
            f"{r['installed_qty']:>12,.4f}  {r['planned_qty']:>12,.4f}  "
            f"{pct_str:>7}  {r['unit']:<8}"
        )

    print(f"\n  {'─'*110}")
    total_inst = sum(r["installed_qty"] for r in results)
    unique_jobs = len({r["job_code"] for r in results})
    print(f"  {len(results)} cost codes with installed qty > 0  |  "
          f"{unique_jobs} jobs  |  "
          f"Total installed: {total_inst:,.4f}")
    print()
    print(f"  HOW TO VERIFY IN HCSS PORTAL:")
    print(f"    1. Go to HeavyJob → Reports → Cost Code Summary (or similar quantity view)")
    print(f"    2. Set date filter: {start} → {end}")
    print(f"    3. Compare 'Quantity' column = our installed_qty")
    print(f"    4. If values match, this endpoint is correct for the budget module")


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Verify installed quantity from HCSS costCode/progress endpoint. "
            "Cross-check against HCSS portal before integrating into budget module."
        )
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--date",  metavar="YYYY-MM-DD",
                       help="Single date (sets both start and end)")
    group.add_argument("--start", metavar="YYYY-MM-DD",
                       help="Range start date")
    parser.add_argument("--end",      metavar="YYYY-MM-DD",
                        help="Range end date (required with --start)")
    parser.add_argument("--job-code", metavar="CODE",
                        help="Filter output to a specific job code, e.g. 25069")
    parser.add_argument("--out",      metavar="FILE",
                        help="Save full JSON output to this file")
    parser.add_argument("--verbose",  action="store_true",
                        help="Show per-job API call details")
    args = parser.parse_args()

    # Resolve dates
    if args.date:
        start = end = args.date
    else:
        if not args.end:
            parser.error("--end is required when using --start")
        start = args.start
        end   = args.end

    print("=" * 70)
    print("  PROGRESS QUANTITY VERIFICATION")
    print("  Endpoint: POST /v1/costCode/progress/advancedRequest")
    print("=" * 70)
    print(f"  Date range : {start} → {end}")
    print(f"  Job filter : {args.job_code or 'all jobs'}")
    print(f"  Output file: {args.out or 'none (console only)'}")
    print()

    client = HCSSClient()

    # Step 1: active jobs
    job_ids = fetch_active_job_ids(client, start, end, args.verbose)
    if not job_ids:
        print("\nNo active jobs found for this date range. Exiting.")
        sys.exit(0)

    # Step 2: job metadata
    job_map = fetch_job_map(client, job_ids, args.verbose)

    # Step 3: planned quantities
    planned = fetch_planned_quantities(client, job_ids, args.verbose)

    # Step 4: installed quantities from progress
    installed = fetch_installed_quantities(client, job_ids, start, end, args.verbose)

    # Build and display results
    print(f"\n[Building] Joining progress data with cost code metadata...")
    results = build_results(installed, planned, job_map, args.job_code)
    print(f"           {len(results)} cost codes with installed qty > 0")

    print_table(results, start, end)

    # Save JSON
    if args.out:
        out_path = Path(args.out)
        out_path.write_text(json.dumps({
            "meta": {
                "start_date":        start,
                "end_date":          end,
                "job_filter":        args.job_code,
                "active_jobs":       len(job_ids),
                "cost_codes_with_installed_qty": len(results),
                "endpoint":          "POST /v1/costCode/progress/advancedRequest",
                "note": (
                    "installed_qty = SUM of 'quantity' field from progress rows "
                    f"for {start} → {end}. "
                    "Compare against HCSS portal 'Quantity' column with same date filter."
                ),
            },
            "results": results,
        }, indent=2))
        print(f"\n  Full output saved → {out_path}")
    else:
        # Default output location
        out_path = Path(__file__).parent / "verify_progress_quantity_output.json"
        out_path.write_text(json.dumps({
            "meta": {
                "start_date":  start,
                "end_date":    end,
                "job_filter":  args.job_code,
                "active_jobs": len(job_ids),
                "cost_codes_with_installed_qty": len(results),
                "endpoint":    "POST /v1/costCode/progress/advancedRequest",
            },
            "results": results,
        }, indent=2))
        print(f"\n  Full output saved → {out_path.name}")


if __name__ == "__main__":
    main()
