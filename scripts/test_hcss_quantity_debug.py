"""
test_hcss_quantity_debug.py
============================
Diagnostic script to capture RAW HCSS API responses for quantity tracking.

Fetches data for job 26004 (Hwy 6 & 24 Shared Path Improvements) and
specifically targets cost code 61950960A "Ex / Lay / BF Waterline 12\"
to compare with the HCSS portal Summary tab values:

  HCSS Portal shows:
    Budgeted Quantity: 1,581.000 LF
    Actual Quantity:   260.000 LF
    Percent Complete:  16.45%

This script calls THREE endpoints and saves raw JSON so you can see
exactly what field names HCSS returns:

  1. /costCodes/advancedRequest     — budget/planned data (already confirmed)
  2. /costCode/progress/advancedRequest — actual quantities (THE KEY ONE)
  3. /changeOrderCostCodesByCostCodes   — change order quantities

Usage:
  cd <project_root>
  python test_hcss_quantity_debug.py

  # With custom date range:
  python test_hcss_quantity_debug.py --start 2026-01-01 --end 2026-06-05

  # For a different job:
  python test_hcss_quantity_debug.py --job-id <HCSS_JOB_UUID>

Output:
  debug_responses/1_cost_codes.json
  debug_responses/2_progress.json
  debug_responses/3_progress_no_dates.json    (cumulative — no date filter)
  debug_responses/4_change_orders.json
  debug_responses/5_comparison_report.txt
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, timedelta
from pathlib import Path

# Ensure repo root is on path
REPO_ROOT = Path(__file__).resolve().parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from dotenv import load_dotenv
load_dotenv(REPO_ROOT / ".env")

from payroll_verification.hcss_client import HCSSClient

# ── Known test data from HCSS portal ──────────────────────────────────────────
# Job 26004: Hwy 6 & 24 Shared Path Improvements
TARGET_JOB_ID = "4e94268c-5630-4b7a-bba6-8000efbb65e1"
TARGET_JOB_CODE = "26004"

# Cost code 61950960A: Ex / Lay / BF Waterline 12"
# From /costCodes/advancedRequest: id = "acfcfe8d-d758-4746-bc36-8aa78c3c437e"
TARGET_COST_CODE_ID = "acfcfe8d-d758-4746-bc36-8aa78c3c437e"
TARGET_COST_CODE = "61950960A"

# HCSS portal values to validate against:
EXPECTED_BUDGETED_QTY = 1581.0  # LF
EXPECTED_ACTUAL_QTY = 260.0     # LF
EXPECTED_PCT_COMPLETE = 16.45   # %

# Also check these from the screenshot:
# 61950960B "Extra Time - 4 Utility Crossings" — Budgeted: 4, Actual: 3, 75%
# 63000016B "Flagging" — Budgeted: 40, Actual: 3, 7.50%
# 62500000A "Construction Surveying" — Budgeted: 1, Actual: 0, 0%

EXTRA_COST_CODE_IDS = {
    "0c0cbbb3-abb9-476e-9838-93ded0b5ee91": ("61950960B", "Extra Time - 4 Utility Crossings", 4.0, 3.0, 75.0),
    "bdfb3cea-e86a-49ad-b65a-12a1acd8cca2": ("63000016B", "Flagging", 40.0, 3.0, 7.50),
    "5a8178a9-1ef4-426e-88eb-0fdc1f579438": ("62500000A", "Construction Surveying", 1.0, 0.0, 0.0),
}


# ── Output directory ──────────────────────────────────────────────────────────
OUTPUT_DIR = Path("debug_responses")


def save_response(filename: str, data: any) -> Path:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUTPUT_DIR / filename
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    size_kb = path.stat().st_size / 1024
    print(f"  ✓ Saved → {path}  ({size_kb:.1f} KB)")
    return path


def fetch_cost_codes(client: HCSSClient, job_id: str) -> list[dict]:
    """Call 1: /costCodes/advancedRequest — budget/planned quantities."""
    print("\n" + "=" * 70)
    print("CALL 1: POST /v2/costCodes/advancedRequest")
    print("  Purpose: Get budgeted/planned quantities per cost code")
    print(f"  Job ID: {job_id}")
    print("=" * 70)

    url = "https://api.hcssapps.com/heavyjob/api/v2/costCodes/advancedRequest"
    payload = {"jobIds": [job_id], "limit": 500}

    all_results = []
    cursor = None
    page = 0

    while True:
        if cursor:
            payload["cursor"] = cursor
        raw = client._request("POST", url, json=payload)
        results = raw.get("results", []) if isinstance(raw, dict) else raw or []
        next_cursor = (raw.get("metadata") or {}).get("nextCursor") if isinstance(raw, dict) else None
        page += 1
        all_results.extend(results)
        print(f"  Page {page}: {len(results)} cost codes")
        if not next_cursor:
            break
        cursor = next_cursor

    print(f"  Total: {len(all_results)} cost codes")
    save_response("1_cost_codes.json", {"total": len(all_results), "results": all_results})
    return all_results


def fetch_progress_with_dates(client: HCSSClient, job_id: str, start: str, end: str) -> list[dict]:
    """Call 2: /costCode/progress/advancedRequest WITH date range."""
    print("\n" + "=" * 70)
    print("CALL 2: POST /v1/costCode/progress/advancedRequest (WITH dates)")
    print("  Purpose: Get actual quantities for date range")
    print(f"  Job ID: {job_id}")
    print(f"  Date range: {start} → {end}")
    print("=" * 70)

    url = f"{client.heavyjob_base}/costCode/progress/advancedRequest"
    payload = {
        "jobId": job_id,
        "startDate": f"{start}T00:00:00Z",
        "endDate": f"{end}T23:59:59Z",
        "limit": 500,
    }

    all_results = []
    cursor = None
    page = 0

    while True:
        if cursor:
            payload["cursor"] = cursor
        raw = client._request("POST", url, json=payload)
        results = raw.get("results", []) if isinstance(raw, dict) else raw or []
        next_cursor = (raw.get("metadata") or {}).get("nextCursor") if isinstance(raw, dict) else None
        page += 1
        all_results.extend(results)
        print(f"  Page {page}: {len(results)} progress records")
        if not next_cursor:
            break
        cursor = next_cursor

    print(f"  Total: {len(all_results)} progress records")

    # Print ALL field names from first record
    if all_results:
        print(f"\n  ── ALL FIELDS IN FIRST RECORD ──")
        first = all_results[0]
        for key, val in sorted(first.items()):
            val_str = str(val)
            if len(val_str) > 80:
                val_str = val_str[:77] + "..."
            print(f"    {key}: {val_str}")

    save_response("2_progress_with_dates.json", {"total": len(all_results), "results": all_results})
    return all_results


def fetch_progress_no_dates(client: HCSSClient, job_id: str) -> list[dict]:
    """Call 3: /costCode/progress/advancedRequest WITHOUT date range (cumulative)."""
    print("\n" + "=" * 70)
    print("CALL 3: POST /v1/costCode/progress/advancedRequest (NO dates)")
    print("  Purpose: Get CUMULATIVE actual quantities (job lifetime)")
    print(f"  Job ID: {job_id}")
    print("=" * 70)

    url = f"{client.heavyjob_base}/costCode/progress/advancedRequest"
    payload = {
        "jobId": job_id,
        "limit": 500,
    }

    all_results = []
    cursor = None
    page = 0

    while True:
        if cursor:
            payload["cursor"] = cursor
        raw = client._request("POST", url, json=payload)
        results = raw.get("results", []) if isinstance(raw, dict) else raw or []
        next_cursor = (raw.get("metadata") or {}).get("nextCursor") if isinstance(raw, dict) else None
        page += 1
        all_results.extend(results)
        print(f"  Page {page}: {len(results)} progress records")
        if not next_cursor:
            break
        cursor = next_cursor

    print(f"  Total: {len(all_results)} progress records")

    if all_results:
        print(f"\n  ── ALL FIELDS IN FIRST RECORD ──")
        first = all_results[0]
        for key, val in sorted(first.items()):
            val_str = str(val)
            if len(val_str) > 80:
                val_str = val_str[:77] + "..."
            print(f"    {key}: {val_str}")

    save_response("3_progress_no_dates.json", {"total": len(all_results), "results": all_results})
    return all_results


def fetch_change_orders(client: HCSSClient, job_id: str, cost_code_ids: list[str]) -> list[dict]:
    """
    Call 4: Try to fetch change order data for cost codes.
    
    The HCSS portal calls:
      POST /heavyjob/api/v1/changeOrderCostCodesByCostCodes
    
    We'll try both the portal URL pattern and the public API pattern.
    """
    print("\n" + "=" * 70)
    print("CALL 4: Change Order Cost Codes")
    print("  Purpose: Get change order quantity adjustments")
    print(f"  Job ID: {job_id}")
    print(f"  Cost code IDs: {len(cost_code_ids)}")
    print("=" * 70)

    # Try the v1 endpoint that the portal uses
    urls_to_try = [
        f"{client.heavyjob_base}/changeOrderCostCodesByCostCodes",
        f"{client.heavyjob_base}/changeOrders/costCodes",
        f"{client.heavyjob_base}/jobs/{job_id}/changeOrders",
    ]

    # Payload patterns to try
    payloads_to_try = [
        # Pattern 1: costCodeIds array (most likely for byCostCodes endpoint)
        {"costCodeIds": cost_code_ids[:20]},
        # Pattern 2: with jobId
        {"jobId": job_id, "costCodeIds": cost_code_ids[:20]},
        # Pattern 3: jobIds array
        {"jobIds": [job_id]},
    ]

    for url in urls_to_try:
        for i, payload in enumerate(payloads_to_try):
            try:
                print(f"\n  Trying: POST {url.split('/api/')[-1]}")
                print(f"  Payload pattern {i + 1}: {list(payload.keys())}")
                raw = client._request("POST", url, json=payload)
                results = raw.get("results", []) if isinstance(raw, dict) else (raw if isinstance(raw, list) else [])
                print(f"  ✓ SUCCESS — {len(results)} records returned")

                if results:
                    print(f"\n  ── ALL FIELDS IN FIRST RECORD ──")
                    first = results[0] if isinstance(results[0], dict) else {}
                    for key, val in sorted(first.items()):
                        val_str = str(val)
                        if len(val_str) > 80:
                            val_str = val_str[:77] + "..."
                        print(f"    {key}: {val_str}")

                save_response("4_change_orders.json", {
                    "url": url,
                    "payload": {k: (v[:3] if isinstance(v, list) else v) for k, v in payload.items()},
                    "total": len(results),
                    "results": results,
                })
                return results

            except Exception as exc:
                error_str = str(exc)
                if "404" in error_str or "405" in error_str:
                    print(f"  ✗ Not found (404/405)")
                elif "400" in error_str:
                    print(f"  ✗ Bad request (400) — wrong payload shape")
                elif "401" in error_str or "403" in error_str:
                    print(f"  ✗ Auth error (401/403)")
                else:
                    print(f"  ✗ Error: {error_str[:100]}")
                continue

    # If all failed, try GET endpoints
    get_urls = [
        f"{client.heavyjob_base}/jobs/{job_id}/changeOrders",
        f"https://api.hcssapps.com/heavyjob/api/v2/jobs/{job_id}/changeOrders",
    ]

    for url in get_urls:
        try:
            print(f"\n  Trying: GET {url.split('/api/')[-1]}")
            raw = client._request("GET", url)
            results = raw.get("results", []) if isinstance(raw, dict) else (raw if isinstance(raw, list) else [])
            print(f"  ✓ SUCCESS — {len(results)} records returned")

            if results:
                print(f"\n  ── ALL FIELDS IN FIRST RECORD ──")
                first = results[0] if isinstance(results[0], dict) else {}
                for key, val in sorted(first.items()):
                    val_str = str(val)
                    if len(val_str) > 80:
                        val_str = val_str[:77] + "..."
                    print(f"    {key}: {val_str}")

            save_response("4_change_orders.json", {
                "url": url,
                "method": "GET",
                "total": len(results),
                "results": results,
            })
            return results

        except Exception as exc:
            print(f"  ✗ Error: {str(exc)[:100]}")
            continue

    print("\n  ⚠ All change order endpoints failed — saving empty result")
    save_response("4_change_orders.json", {
        "error": "All endpoints failed",
        "urls_tried": urls_to_try + get_urls,
        "results": [],
    })
    return []


def build_comparison_report(
    cost_codes: list[dict],
    progress_dated: list[dict],
    progress_cumulative: list[dict],
    change_orders: list[dict],
) -> str:
    """
    Build a text comparison report for the target cost code(s),
    comparing all API values with HCSS portal values.
    """
    lines = []
    lines.append("=" * 80)
    lines.append("QUANTITY DATA COMPARISON REPORT")
    lines.append("=" * 80)
    lines.append("")

    # Build lookup maps
    cc_map = {}  # cost_code_id → cost code dict
    for cc in cost_codes:
        ccid = cc.get("id")
        if ccid:
            cc_map[ccid] = cc

    # Progress lookups — by costCodeId field
    progress_dated_map = {}
    for p in progress_dated:
        ccid = p.get("costCodeId") or p.get("id")
        if ccid:
            progress_dated_map[ccid] = p

    progress_cumul_map = {}
    for p in progress_cumulative:
        ccid = p.get("costCodeId") or p.get("id")
        if ccid:
            progress_cumul_map[ccid] = p

    # Targets to check
    targets = {TARGET_COST_CODE_ID: (TARGET_COST_CODE, "Ex / Lay / BF Waterline 12\"", EXPECTED_BUDGETED_QTY, EXPECTED_ACTUAL_QTY, EXPECTED_PCT_COMPLETE)}
    targets.update(EXTRA_COST_CODE_IDS)

    for ccid, (code, desc, exp_budget_qty, exp_actual_qty, exp_pct) in targets.items():
        lines.append(f"── {code} — {desc} ──")
        lines.append(f"  HCSS Portal Expected:")
        lines.append(f"    Budgeted Quantity: {exp_budget_qty}")
        lines.append(f"    Actual Quantity:   {exp_actual_qty}")
        lines.append(f"    Percent Complete:  {exp_pct}%")
        lines.append("")

        # From /costCodes
        cc = cc_map.get(ccid, {})
        if cc:
            lines.append(f"  From /costCodes/advancedRequest:")
            lines.append(f"    quantity:          {cc.get('quantity', 'N/A')}")
            lines.append(f"    unitOfMeasure:     {cc.get('unitOfMeasure', 'N/A')}")
            match = "✓" if cc.get("quantity") == exp_budget_qty else "✗ MISMATCH"
            lines.append(f"    Budget match:      {match}")
        else:
            lines.append(f"  From /costCodes: NOT FOUND for id={ccid}")
        lines.append("")

        # From progress (with dates)
        p_dated = progress_dated_map.get(ccid, {})
        if p_dated:
            lines.append(f"  From /progress (WITH dates):")
            # Print all quantity-related fields
            for key in sorted(p_dated.keys()):
                if any(q in key.lower() for q in ["quantity", "qty", "todate", "cost", "progress", "percent", "complete"]):
                    lines.append(f"    {key}: {p_dated[key]}")
        else:
            lines.append(f"  From /progress (WITH dates): NOT FOUND")
        lines.append("")

        # From progress (cumulative)
        p_cumul = progress_cumul_map.get(ccid, {})
        if p_cumul:
            lines.append(f"  From /progress (NO dates — cumulative):")
            for key in sorted(p_cumul.keys()):
                if any(q in key.lower() for q in ["quantity", "qty", "todate", "cost", "progress", "percent", "complete"]):
                    lines.append(f"    {key}: {p_cumul[key]}")
        else:
            lines.append(f"  From /progress (NO dates): NOT FOUND")
        lines.append("")
        lines.append("-" * 60)
        lines.append("")

    # Field name discovery
    lines.append("")
    lines.append("=" * 80)
    lines.append("FIELD NAME DISCOVERY — Progress Endpoint")
    lines.append("=" * 80)

    if progress_dated:
        lines.append("")
        lines.append("All fields in progress response (WITH dates):")
        first = progress_dated[0]
        for key in sorted(first.keys()):
            lines.append(f"  {key}: {type(first[key]).__name__} = {str(first[key])[:60]}")

    if progress_cumulative:
        lines.append("")
        lines.append("All fields in progress response (NO dates):")
        first = progress_cumulative[0]
        for key in sorted(first.keys()):
            lines.append(f"  {key}: {type(first[key]).__name__} = {str(first[key])[:60]}")

    if progress_dated and progress_cumulative:
        # Compare field sets
        dated_keys = set(progress_dated[0].keys())
        cumul_keys = set(progress_cumulative[0].keys())
        only_dated = dated_keys - cumul_keys
        only_cumul = cumul_keys - dated_keys
        if only_dated:
            lines.append(f"\n  Fields ONLY in dated response: {only_dated}")
        if only_cumul:
            lines.append(f"\n  Fields ONLY in cumulative response: {only_cumul}")
        if not only_dated and not only_cumul:
            lines.append(f"\n  Both responses have identical field names.")

    report = "\n".join(lines)
    return report


def main():
    parser = argparse.ArgumentParser(description="Debug HCSS quantity tracking data")
    parser.add_argument("--job-id", default=TARGET_JOB_ID, help="HCSS job UUID")
    parser.add_argument("--start", default=None, help="Start date YYYY-MM-DD (default: 6 months ago)")
    parser.add_argument("--end", default=None, help="End date YYYY-MM-DD (default: today)")
    args = parser.parse_args()

    today = date.today()
    start_date = args.start or (today - timedelta(days=180)).isoformat()
    end_date = args.end or today.isoformat()
    job_id = args.job_id

    print("=" * 70)
    print("HCSS QUANTITY TRACKING DEBUG SCRIPT")
    print("=" * 70)
    print(f"  Job ID:     {job_id}")
    print(f"  Date range: {start_date} → {end_date}")
    print(f"  Target CC:  {TARGET_COST_CODE} ({TARGET_COST_CODE_ID[:12]}...)")
    print(f"  Expected:   Budget={EXPECTED_BUDGETED_QTY} Actual={EXPECTED_ACTUAL_QTY} Pct={EXPECTED_PCT_COMPLETE}%")
    print(f"  Output dir: {OUTPUT_DIR.resolve()}")
    print("=" * 70)

    client = HCSSClient()

    # Call 1: Cost codes (budget/planned)
    cost_codes = fetch_cost_codes(client, job_id)

    # Call 2: Progress WITH dates (what your tracker currently uses)
    progress_dated = fetch_progress_with_dates(client, job_id, start_date, end_date)

    # Call 3: Progress WITHOUT dates (cumulative — what portal likely uses)
    progress_cumulative = fetch_progress_no_dates(client, job_id)

    # Call 4: Change orders
    all_cc_ids = [cc.get("id") for cc in cost_codes if cc.get("id")]
    change_orders = fetch_change_orders(client, job_id, all_cc_ids)

    # Build comparison report
    report = build_comparison_report(cost_codes, progress_dated, progress_cumulative, change_orders)
    print("\n" + report)

    # Save report
    report_path = OUTPUT_DIR / "5_comparison_report.txt"
    with open(report_path, "w") as f:
        f.write(report)
    print(f"\n  ✓ Comparison report saved → {report_path}")

    print("\n" + "=" * 70)
    print("DONE — Check the debug_responses/ directory for raw JSON files.")
    print("Share these files and the comparison report in our next message.")
    print("=" * 70)


if __name__ == "__main__":
    main()