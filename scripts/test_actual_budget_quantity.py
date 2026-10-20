"""
test_quantity_range_actual.py

Deep HCSS Quantity Test Script — Date Range Actual Quantity

Purpose:
    Test installed / actual quantity against HCSS HeavyJob:
    Manager > Reports > Cost Code Reports > Cost Code Summary.

Important finding:
    Based on HCSS report behavior:
        2026-05-01 -> 2026-05-31 = 366.220
        2026-06-01 -> 2026-06-15 = 677.670

    The HCSS report is showing selected-date-range Actual Quantity,
    not full project-to-date quantity.

Correct matching logic:
    Budgeted Quantity:
        From costCodes/advancedRequest

    Actual Quantity:
        SUM of costCode/progress/advancedRequest field "quantity"
        inside the SAME selected date range.

    Percent Complete:
        actual_quantity / budgeted_quantity * 100

Do NOT use quantityToDate as the main value for this report.
quantityToDate is cumulative and can cause mismatch if used for date-range reports.

Usage:
    python test_quantity_range_actual.py --start 2026-06-01 --end 2026-06-15

    python test_quantity_range_actual.py --start 2026-06-01 --end 2026-06-15 --job-code 25001 --cost-code 14011111

    python test_quantity_range_actual.py --start 2026-06-01 --end 2026-06-15 --job-id 5b5a926f-2f21-4728-a531-80fc692d1c1c --cost-code 14011111

    python test_quantity_range_actual.py --start 2026-05-01 --end 2026-05-31 --job-code 25001 --cost-code 14011111 --out may_test.json

Place this file in your project root where .env exists.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv


# ---------------------------------------------------------------------------
# Project setup
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parent

if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

load_dotenv(_REPO_ROOT / ".env")

from payroll_verification.hcss_client import HCSSClient  # noqa: E402


# ---------------------------------------------------------------------------
# HCSS endpoints
# ---------------------------------------------------------------------------

COST_CODES_URL = "https://api.hcssapps.com/heavyjob/api/v2/costCodes/advancedRequest"
PROGRESS_URL = "https://api.hcssapps.com/heavyjob/api/v1/costCode/progress/advancedRequest"

ALERT_THRESHOLD = 75.0


# ---------------------------------------------------------------------------
# General helpers
# ---------------------------------------------------------------------------

def safe_float(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def normalize_date(value: Any) -> str:
    if not value:
        return ""
    return str(value).replace("Z", "")


def pick_record_date(row: dict[str, Any]) -> str:
    for key in (
        "date",
        "businessDate",
        "timeCardDate",
        "workDate",
        "entryDate",
        "createdDateTime",
        "lastModifiedDateTime",
        "updatedAt",
    ):
        if row.get(key):
            return normalize_date(row.get(key))
    return ""


def extract_job_id(row: dict[str, Any]) -> str:
    return row.get("jobId") or row.get("job_id") or ""


def extract_cost_code_id(row: dict[str, Any]) -> str:
    return (
        row.get("costCodeId")
        or row.get("cost_code_id")
        or row.get("costCodeID")
        or row.get("id")
        or ""
    )


def extract_cost_code_number(row: dict[str, Any]) -> str:
    return (
        row.get("code")
        or row.get("costCode")
        or row.get("number")
        or row.get("costCodeCode")
        or ""
    )


def extract_description(row: dict[str, Any]) -> str:
    return (
        row.get("description")
        or row.get("name")
        or row.get("costCodeDescription")
        or ""
    )


def extract_unit(row: dict[str, Any]) -> str:
    return (
        row.get("unit")
        or row.get("unitOfMeasure")
        or row.get("uom")
        or ""
    )


def get_planned_quantity(cost_code: dict[str, Any]) -> float:
    return safe_float(
        cost_code.get("quantity")
        or cost_code.get("plannedQuantity")
        or cost_code.get("budgetQuantity")
        or cost_code.get("budgetedQuantity")
    )


# ---------------------------------------------------------------------------
# HCSS pagination helper
# ---------------------------------------------------------------------------

def fetch_all_pages(
    client: HCSSClient,
    method: str,
    url: str,
    payload: dict[str, Any],
    *,
    label: str,
    page_delay: float = 0.75,
    max_retries: int = 8,
) -> list[dict[str, Any]]:
    all_results: list[dict[str, Any]] = []
    cursor: str | None = None
    page = 0

    while True:
        req_payload = dict(payload)

        if cursor:
            req_payload["cursor"] = cursor

        retries = 0

        while True:
            try:
                if method.upper() == "POST":
                    raw = client._request(method, url, json=req_payload)
                else:
                    raw = client._request(method, url, params=req_payload)
                break

            except requests.exceptions.HTTPError as exc:
                status = exc.response.status_code if exc.response is not None else None

                if status == 429 and retries < max_retries:
                    retry_after = None

                    if exc.response is not None:
                        retry_after = exc.response.headers.get("Retry-After")

                    wait_seconds = (
                        float(retry_after)
                        if retry_after
                        else min(60.0, (2 ** retries) * 5.0)
                    )

                    retries += 1

                    print(
                        f"    429 rate limit. Waiting {wait_seconds:.1f}s "
                        f"(retry {retries}/{max_retries}) [{label}]"
                    )

                    time.sleep(wait_seconds)
                    continue

                raise

        results, next_cursor = client._extract_results_and_cursor(raw)

        page += 1
        all_results.extend(results)

        print(
            f"    [{label}] page {page}: {len(results)} rows "
            f"(total: {len(all_results)})"
        )

        if not next_cursor:
            break

        cursor = next_cursor
        time.sleep(page_delay)

    return all_results


# ---------------------------------------------------------------------------
# HCSS fetch functions
# ---------------------------------------------------------------------------

def fetch_timecards(
    client: HCSSClient,
    start_date: str,
    end_date: str,
) -> list[dict[str, Any]]:
    print(f"\n[1] Fetching timecards to discover active jobs: {start_date} -> {end_date}")

    rows = client.fetch_timecards(
        start_date=start_date,
        end_date=end_date,
    )

    print(f"    Timecards found: {len(rows)}")

    return rows


def fetch_job_details(
    client: HCSSClient,
    job_ids: list[str],
) -> tuple[dict[str, str], dict[str, str], list[dict[str, Any]]]:
    print("\n[2] Fetching job details from /jobs/advanced")

    if not job_ids:
        return {}, {}, []

    try:
        raw = client._request(
            "POST",
            f"{client.heavyjob_base}/jobs/advanced",
            json={"jobIds": job_ids},
        )

        job_list = raw if isinstance(raw, list) else raw.get("results", [])

    except Exception as exc:
        print(f"    WARNING: /jobs/advanced failed: {exc}")
        job_list = []

    job_code_map: dict[str, str] = {}
    job_name_map: dict[str, str] = {}

    for job in job_list:
        jid = job.get("id")

        if not jid:
            continue

        code = (
            job.get("code")
            or job.get("jobCode")
            or job.get("number")
            or jid[:8]
        )

        name = (
            job.get("description")
            or job.get("name")
            or code
        )

        job_code_map[jid] = code
        job_name_map[jid] = name

    for jid in job_ids:
        job_code_map.setdefault(jid, jid[:8])
        job_name_map.setdefault(jid, "")

    print(f"    Jobs resolved: {len(job_code_map)}")

    return job_code_map, job_name_map, job_list


def fetch_cost_codes(
    client: HCSSClient,
    job_ids: list[str],
) -> list[dict[str, Any]]:
    print("\n[3] Fetching cost codes from costCodes/advancedRequest")

    if not job_ids:
        return []

    payload = {
        "jobIds": job_ids,
        "limit": 500,
    }

    rows = fetch_all_pages(
        client,
        "POST",
        COST_CODES_URL,
        payload,
        label="cost codes",
        page_delay=0.75,
    )

    print(f"    Cost code records found: {len(rows)}")

    return rows


def fetch_progress_for_job_range(
    client: HCSSClient,
    job_id: str,
    start_date: str,
    end_date: str,
) -> list[dict[str, Any]]:
    """
    Fetch CostCodeProgress records for the SAME selected report date range.

    This is important:
        HCSS Cost Code Summary actual quantity changes when date range changes.
        So we must fetch progress only inside that selected date range.

    Main field:
        quantity = daily / period production quantity

    Do not use:
        quantityToDate as the primary report value.
    """
    payload = {
        "jobId": job_id,
        "startDate": f"{start_date}T00:00:00Z",
        "endDate": f"{end_date}T23:59:59Z",
        "limit": 500,
    }

    return fetch_all_pages(
        client,
        "POST",
        PROGRESS_URL,
        payload,
        label=f"progress {job_id[:8]}",
        page_delay=1.0,
    )


# ---------------------------------------------------------------------------
# Installed quantity calculations
# ---------------------------------------------------------------------------

def calculate_actual_quantity_variants(
    progress_rows: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """
    Calculate multiple variants for comparison.

    Correct for your HCSS report:
        actual_quantity = SUM(progress.quantity) inside selected date range.

    Extra values are included for debugging:
        latest_quantity_to_date
        max_quantity_to_date
        bad_sum_quantity_to_date_do_not_use
    """
    sum_range_quantity: dict[str, float] = defaultdict(float)

    latest_quantity_to_date: dict[str, tuple[str, float]] = {}
    max_quantity_to_date: dict[str, float] = defaultdict(float)
    bad_sum_quantity_to_date: dict[str, float] = defaultdict(float)

    progress_count: dict[str, int] = defaultdict(int)
    fields_seen_by_cc: dict[str, set[str]] = defaultdict(set)

    raw_progress_by_cc: dict[str, list[dict[str, Any]]] = defaultdict(list)

    for row in progress_rows:
        ccid = extract_cost_code_id(row)

        if not ccid:
            continue

        progress_count[ccid] += 1
        fields_seen_by_cc[ccid].update(row.keys())
        raw_progress_by_cc[ccid].append(row)

        # Correct value for selected report range.
        range_qty = safe_float(row.get("quantity"))
        sum_range_quantity[ccid] += range_qty

        # Debug only.
        cumulative_raw = (
            row.get("quantityToDate")
            if row.get("quantityToDate") is not None
            else row.get("actualQuantityToDate")
            if row.get("actualQuantityToDate") is not None
            else row.get("actualQuantity")
            if row.get("actualQuantity") is not None
            else row.get("installedQuantity")
        )

        if cumulative_raw is not None:
            cumulative_qty = safe_float(cumulative_raw)

            record_date = pick_record_date(row)

            old = latest_quantity_to_date.get(ccid)

            if old is None or record_date >= old[0]:
                latest_quantity_to_date[ccid] = (record_date, cumulative_qty)

            max_quantity_to_date[ccid] = max(
                max_quantity_to_date[ccid],
                cumulative_qty,
            )

            # Intentionally wrong if cumulative.
            # Kept only to prove double counting.
            bad_sum_quantity_to_date[ccid] += cumulative_qty

    all_ccids = set()
    all_ccids.update(sum_range_quantity.keys())
    all_ccids.update(latest_quantity_to_date.keys())
    all_ccids.update(max_quantity_to_date.keys())
    all_ccids.update(bad_sum_quantity_to_date.keys())

    results: dict[str, dict[str, Any]] = {}

    for ccid in all_ccids:
        latest_date, latest_qty = latest_quantity_to_date.get(ccid, ("", 0.0))

        results[ccid] = {
            "progress_record_count": progress_count.get(ccid, 0),

            # Main value
            "actual_quantity_range_sum": round(sum_range_quantity.get(ccid, 0.0), 4),
            "recommended_installed_quantity": round(sum_range_quantity.get(ccid, 0.0), 4),
            "recommended_source": "SUM progress.quantity inside selected date range",

            # Debug values
            "latest_quantity_to_date": round(latest_qty, 4),
            "latest_quantity_to_date_record_date": latest_date,
            "max_quantity_to_date": round(max_quantity_to_date.get(ccid, 0.0), 4),
            "bad_sum_quantity_to_date_do_not_use": round(
                bad_sum_quantity_to_date.get(ccid, 0.0),
                4,
            ),

            "fields_seen": sorted(fields_seen_by_cc.get(ccid, set())),
        }

    return results


# ---------------------------------------------------------------------------
# Build final report rows
# ---------------------------------------------------------------------------

def build_rows(
    cost_codes: list[dict[str, Any]],
    installed_variants: dict[str, dict[str, Any]],
    job_code_map: dict[str, str],
    job_name_map: dict[str, str],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []

    for cc in cost_codes:
        ccid = cc.get("id") or cc.get("costCodeId") or ""

        if not ccid:
            continue

        planned_quantity = get_planned_quantity(cc)

        if planned_quantity <= 0:
            continue

        jid = extract_job_id(cc)

        variants = installed_variants.get(ccid, {})

        installed_quantity = safe_float(
            variants.get("recommended_installed_quantity")
        )

        percent_complete = (
            round((installed_quantity / planned_quantity) * 100, 2)
            if planned_quantity > 0
            else 0.0
        )

        remaining_quantity = round(
            max(0.0, planned_quantity - installed_quantity),
            4,
        )

        if percent_complete >= 100.0:
            status = "OVER_RISK"
        elif percent_complete >= ALERT_THRESHOLD:
            status = "NEAR_COMPLETION"
        else:
            status = "ON_TRACK"

        rows.append(
            {
                "job_id": jid,
                "job_code": job_code_map.get(jid, jid[:8] if jid else "Unknown"),
                "job_name": job_name_map.get(jid, ""),

                "cost_code_id": ccid,
                "cost_code": extract_cost_code_number(cc) or ccid[:8],
                "description": extract_description(cc),
                "unit": extract_unit(cc),

                "planned_quantity": round(planned_quantity, 4),
                "installed_quantity": round(installed_quantity, 4),
                "actual_quantity_range_sum": variants.get("actual_quantity_range_sum", 0.0),
                "remaining_quantity": remaining_quantity,
                "percent_complete": percent_complete,

                "status": status,
                "alert": percent_complete >= ALERT_THRESHOLD,

                "recommended_source": variants.get(
                    "recommended_source",
                    "no progress records",
                ),

                # Debug fields
                "latest_quantity_to_date": variants.get("latest_quantity_to_date", 0.0),
                "latest_quantity_to_date_record_date": variants.get(
                    "latest_quantity_to_date_record_date",
                    "",
                ),
                "max_quantity_to_date": variants.get("max_quantity_to_date", 0.0),
                "bad_sum_quantity_to_date_do_not_use": variants.get(
                    "bad_sum_quantity_to_date_do_not_use",
                    0.0,
                ),
                "progress_record_count": variants.get("progress_record_count", 0),
                "progress_fields_seen": variants.get("fields_seen", []),
            }
        )

    rows.sort(key=lambda row: (row["job_code"], row["cost_code"], row["description"]))

    return rows


def filter_rows(
    rows: list[dict[str, Any]],
    *,
    job_code: str | None,
    cost_code: str | None,
    description_contains: str | None,
) -> list[dict[str, Any]]:
    filtered = rows

    if job_code:
        filtered = [
            row for row in filtered
            if str(row.get("job_code")) == str(job_code)
        ]

    if cost_code:
        filtered = [
            row for row in filtered
            if str(row.get("cost_code")) == str(cost_code)
        ]

    if description_contains:
        needle = description_contains.lower()

        filtered = [
            row for row in filtered
            if needle in str(row.get("description", "")).lower()
        ]

    return filtered


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fieldnames = [
        "job_code",
        "job_name",
        "cost_code",
        "description",
        "unit",
        "planned_quantity",
        "installed_quantity",
        "actual_quantity_range_sum",
        "remaining_quantity",
        "percent_complete",
        "status",
        "recommended_source",
        "latest_quantity_to_date",
        "latest_quantity_to_date_record_date",
        "max_quantity_to_date",
        "bad_sum_quantity_to_date_do_not_use",
        "progress_record_count",
        "job_id",
        "cost_code_id",
    ]

    with path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()

        for row in rows:
            writer.writerow({key: row.get(key, "") for key in fieldnames})


def print_focus_summary(rows: list[dict[str, Any]]) -> None:
    print("\nFocused / sample result rows")
    print("-" * 120)

    if not rows:
        print("No matching rows found.")
        return

    for row in rows[:50]:
        print(
            f"Job {row['job_code']} | Cost Code {row['cost_code']} | "
            f"{row['description']} | Unit {row['unit']}"
        )
        print(f"  Budgeted Quantity              : {row['planned_quantity']}")
        print(f"  Installed / Actual Quantity    : {row['installed_quantity']}")
        print(f"  Actual Range SUM quantity      : {row['actual_quantity_range_sum']}")
        print(f"  Recommended Source             : {row['recommended_source']}")
        print(f"  Percent Complete               : {row['percent_complete']}%")
        print(f"  Remaining Quantity             : {row['remaining_quantity']}")
        print(f"  Latest quantityToDate DEBUG    : {row['latest_quantity_to_date']}")
        print(f"  Max quantityToDate DEBUG       : {row['max_quantity_to_date']}")
        print(f"  BAD sum quantityToDate DEBUG   : {row['bad_sum_quantity_to_date_do_not_use']}")
        print(f"  Progress Records               : {row['progress_record_count']}")
        print(f"  Progress Fields Seen           : {', '.join(row.get('progress_fields_seen', []))}")
        print("-" * 120)


# ---------------------------------------------------------------------------
# Main run
# ---------------------------------------------------------------------------

def run(args: argparse.Namespace) -> dict[str, Any]:
    client = HCSSClient()

    # -----------------------------------------------------------------------
    # Step 1: Resolve selected jobs
    # -----------------------------------------------------------------------
    if args.job_id:
        print(f"\n[1] Using provided job_id: {args.job_id}")
        timecards: list[dict[str, Any]] = []
        job_ids = [args.job_id]
    else:
        timecards = fetch_timecards(client, args.start, args.end)

        job_ids = sorted(
            {
                tc.get("jobId")
                for tc in timecards
                if tc.get("jobId")
            }
        )

    print(f"    Job IDs selected: {len(job_ids)}")

    if not job_ids:
        print("\nNo job IDs found from timecards.")
        print("If the HCSS report row exists but no timecard exists in this date range,")
        print("run again using --job-id with the exact HCSS job UUID.")

    # -----------------------------------------------------------------------
    # Step 2: Job details
    # -----------------------------------------------------------------------
    job_code_map, job_name_map, raw_jobs = fetch_job_details(client, job_ids)

    if args.job_code and not args.job_id:
        matched_job_ids = [
            jid for jid, code in job_code_map.items()
            if str(code) == str(args.job_code)
        ]

        if matched_job_ids:
            print(
                f"\nFiltering selected jobs to job_code={args.job_code}: "
                f"{len(matched_job_ids)} job(s)"
            )
            job_ids = matched_job_ids
        else:
            print(
                f"\nWARNING: job_code={args.job_code} was not found among active jobs."
            )
            print(
                "If this job appears in HCSS Cost Code Summary, run again with --job-id."
            )

    # -----------------------------------------------------------------------
    # Step 3: Cost codes
    # -----------------------------------------------------------------------
    raw_cost_codes = fetch_cost_codes(client, job_ids)

    # -----------------------------------------------------------------------
    # Step 4: Progress inside selected date range
    # -----------------------------------------------------------------------
    print(
        f"\n[4] Fetching progress from selected range: "
        f"{args.start} -> {args.end}"
    )
    print("    Installed quantity will be SUM(progress.quantity) inside this range.")

    all_progress: list[dict[str, Any]] = []

    for index, jid in enumerate(job_ids, start=1):
        job_code = job_code_map.get(jid, jid[:8])

        print(f"\n    Job {index}/{len(job_ids)}: {job_code} ({jid})")

        try:
            progress_rows = fetch_progress_for_job_range(
                client,
                jid,
                args.start,
                args.end,
            )
            all_progress.extend(progress_rows)

        except Exception as exc:
            print(f"    WARNING: progress fetch failed for job {job_code}: {exc}")

        if index < len(job_ids):
            time.sleep(args.job_delay)

    print(f"\n    Total progress records fetched: {len(all_progress)}")

    # -----------------------------------------------------------------------
    # Step 5: Calculate actual quantities
    # -----------------------------------------------------------------------
    installed_variants = calculate_actual_quantity_variants(all_progress)

    rows = build_rows(
        cost_codes=raw_cost_codes,
        installed_variants=installed_variants,
        job_code_map=job_code_map,
        job_name_map=job_name_map,
    )

    focused_rows = filter_rows(
        rows,
        job_code=args.job_code,
        cost_code=args.cost_code,
        description_contains=args.description,
    )

    focused_ccids = {row["cost_code_id"] for row in focused_rows}

    focused_progress_records = [
        row for row in all_progress
        if extract_cost_code_id(row) in focused_ccids
    ]

    # -----------------------------------------------------------------------
    # Build output
    # -----------------------------------------------------------------------
    output = {
        "fetched_at": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "period_start": args.start,
        "period_end": args.end,
        "filters": {
            "job_id": args.job_id,
            "job_code": args.job_code,
            "cost_code": args.cost_code,
            "description": args.description,
        },
        "api_sources": {
            "timecards": f"{client.heavyjob_base}/timeCardInfo",
            "jobs_advanced": f"{client.heavyjob_base}/jobs/advanced",
            "cost_codes": COST_CODES_URL,
            "progress": PROGRESS_URL,
        },
        "calculation_rule": {
            "planned_quantity": "costCodes/advancedRequest quantity/plannedQuantity/budgetQuantity/budgetedQuantity",
            "installed_quantity": "SUM costCode/progress/advancedRequest field 'quantity' inside selected start/end range",
            "percent_complete": "installed_quantity / planned_quantity * 100",
            "do_not_use_for_main_value": "quantityToDate because this report is selected-date-range based",
        },
        "summary": {
            "jobs_selected": len(job_ids),
            "timecards_count": len(timecards),
            "cost_codes_count": len(raw_cost_codes),
            "progress_records_count": len(all_progress),
            "rows_count": len(rows),
            "focused_rows_count": len(focused_rows),
        },
        "rows": rows,
        "focused_rows": focused_rows,
        "raw": {
            "jobs": raw_jobs,
            "cost_codes": raw_cost_codes,
            "progress_records_count": len(all_progress),
            "focused_progress_records": focused_progress_records,
        },
    }

    return output


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Test HCSS quantity actual values using selected date range."
    )

    parser.add_argument(
        "--start",
        required=True,
        help="Report start date YYYY-MM-DD",
    )

    parser.add_argument(
        "--end",
        required=True,
        help="Report end date YYYY-MM-DD",
    )

    parser.add_argument(
        "--job-id",
        default=None,
        help="Exact HCSS job UUID. Best for single-job testing.",
    )

    parser.add_argument(
        "--job-code",
        default=None,
        help="Human job code, e.g. 25001",
    )

    parser.add_argument(
        "--cost-code",
        default=None,
        help="Cost code number, e.g. 14011111",
    )

    parser.add_argument(
        "--description",
        default=None,
        help='Filter by description text, e.g. "Pour Zone 6"',
    )

    parser.add_argument(
        "--out",
        default=None,
        help="Output JSON file",
    )

    parser.add_argument(
        "--csv",
        default=None,
        help="Output CSV file",
    )

    parser.add_argument(
        "--job-delay",
        type=float,
        default=1.5,
        help="Delay between progress job calls to reduce rate limit risk.",
    )

    args = parser.parse_args()

    for label, value in [("--start", args.start), ("--end", args.end)]:
        try:
            datetime.strptime(value, "%Y-%m-%d")
        except ValueError:
            print(f"ERROR: invalid date for {label}: {value}. Expected YYYY-MM-DD.")
            sys.exit(1)

    if args.end < args.start:
        print("ERROR: --end must be on or after --start.")
        sys.exit(1)

    out_json = Path(
        args.out
        or f"quantity_range_actual_{args.start}_{args.end}.json"
    )

    out_csv = Path(
        args.csv
        or out_json.with_suffix(".csv")
    )

    print("\n" + "=" * 80)
    print("HCSS Quantity Range Actual Quantity Test")
    print("=" * 80)
    print(f"Report period       : {args.start} -> {args.end}")
    print(f"JSON output         : {out_json}")
    print(f"CSV output          : {out_csv}")
    print(f"Job ID filter       : {args.job_id or '-'}")
    print(f"Job code filter     : {args.job_code or '-'}")
    print(f"Cost code filter    : {args.cost_code or '-'}")
    print(f"Description filter  : {args.description or '-'}")
    print("Installed rule      : SUM progress.quantity inside selected date range")
    print("=" * 80)

    data = run(args)

    out_json.write_text(
        json.dumps(data, indent=2, default=str),
        encoding="utf-8",
    )

    rows_for_csv = data["focused_rows"] or data["rows"]
    write_csv(out_csv, rows_for_csv)

    print_focus_summary(rows_for_csv[:50])

    print("\n" + "=" * 80)
    print("Done")
    print("=" * 80)
    print(f"Jobs selected          : {data['summary']['jobs_selected']}")
    print(f"Timecards count        : {data['summary']['timecards_count']}")
    print(f"Cost codes count       : {data['summary']['cost_codes_count']}")
    print(f"Progress records count : {data['summary']['progress_records_count']}")
    print(f"Rows count             : {data['summary']['rows_count']}")
    print(f"Focused rows count     : {data['summary']['focused_rows_count']}")
    print(f"Saved JSON             : {out_json.resolve()}")
    print(f"Saved CSV              : {out_csv.resolve()}")
    print("=" * 80)

    print("\nCompare with HCSS Cost Code Summary:")
    print("1. Use the same report date range in HCSS.")
    print("2. Match Job Code and Cost Code.")
    print("3. Compare HCSS Actual Quantity with installed_quantity.")
    print("4. The value should match SUM progress.quantity for that selected date range.")
    print("5. If HCSS row exists but script finds no job, re-run with --job-id.")
    print("6. If installed_quantity is still different, inspect raw.focused_progress_records in JSON.")


if __name__ == "__main__":
    main()