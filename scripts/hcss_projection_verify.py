"""
hcss_projection_verify.py

HCSS Projection Verification Script

Purpose:
    Reproduce the numbers shown in HCSS HeavyJob > Reports > Job Reports >
    Production Analysis for a selected date range.

Correct logic:
    Budgeted Quantity -> costCodes/advancedRequest field "quantity"
    Quantity          -> SUM costCode/progress/advancedRequest field "quantity" in selected range
    Completion %      -> Quantity / Budgeted Quantity
    Budgeted Cost     -> laborDollars + equipmentDollars + materialDollars + subcontractDollars
    Expected          -> Budgeted Cost * Completion %
    Actual            -> SUM HCSS jobCosts/advancedRequest cost fields:
                         laborCost + equipmentCost + materialCost + subcontractCost + truckingCost
    Variance          -> Expected - Actual

Usage:
    python hcss_projection_verify.py --start 2026-05-31 --end 2026-06-06
    python hcss_projection_verify.py --start 2026-05-31 --end 2026-06-06 --job-code 25006
    python hcss_projection_verify.py --start 2026-05-31 --end 2026-06-06 --job-code 25006 --check-cost-code 22711111
    python hcss_projection_verify.py --start 2026-05-31 --end 2026-06-06 --job-id <uuid>

Place this file in your project root, alongside payroll_verification/, where .env exists.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

_REPO_ROOT = Path(__file__).resolve().parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

load_dotenv(_REPO_ROOT / ".env")

from payroll_verification.hcss_client import HCSSClient  # noqa: E402


logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("hcss_projection_verify")


COST_CODES_URL = "https://api.hcssapps.com/heavyjob/api/v2/costCodes/advancedRequest"
PROGRESS_URL = "https://api.hcssapps.com/heavyjob/api/v1/costCode/progress/advancedRequest"
JOB_COSTS_URL = "https://api.hcssapps.com/heavyjob/api/v1/jobCosts/advancedRequest"

BUDGET_DOLLAR_FIELDS = [
    "laborDollars",
    "equipmentDollars",
    "materialDollars",
    "subcontractDollars",
]

ACTUAL_COST_FIELDS = [
    "laborCost",
    "equipmentCost",
    "materialCost",
    "subcontractCost",
    "truckingCost",
]

OUTPUT_DIR = Path("./hcss_verification_output")
OUTPUT_DIR.mkdir(exist_ok=True)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def safe_float(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def normalize_code(value: Any) -> str:
    return str(value or "").strip()


def extract_job_id(row: dict[str, Any]) -> str:
    return (
        row.get("jobId")
        or row.get("job_id")
        or row.get("job", {}).get("id")
        or ""
    )


def extract_cost_code_id(row: dict[str, Any]) -> str:
    return (
        row.get("costCodeId")
        or row.get("cost_code_id")
        or row.get("id")
        or row.get("costCode", {}).get("id")
        or row.get("costCode", {}).get("costCodeId")
        or ""
    )


def get_planned_quantity(cost_code: dict[str, Any]) -> float:
    return safe_float(
        cost_code.get("quantity")
        or cost_code.get("plannedQuantity")
        or cost_code.get("budgetQuantity")
        or cost_code.get("budgetedQuantity")
    )


def get_budgeted_cost(cost_code: dict[str, Any]) -> float:
    return sum(safe_float(cost_code.get(field)) for field in BUDGET_DOLLAR_FIELDS)


def get_actual_cost_from_job_cost_row(row: dict[str, Any]) -> dict[str, float]:
    labor_cost = safe_float(row.get("laborCost"))
    equipment_cost = safe_float(row.get("equipmentCost"))
    material_cost = safe_float(row.get("materialCost"))
    subcontract_cost = safe_float(row.get("subcontractCost"))
    trucking_cost = safe_float(row.get("truckingCost"))

    actual_cost = (
        labor_cost
        + equipment_cost
        + material_cost
        + subcontract_cost
        + trucking_cost
    )

    return {
        "laborCost": labor_cost,
        "equipmentCost": equipment_cost,
        "materialCost": material_cost,
        "subcontractCost": subcontract_cost,
        "truckingCost": trucking_cost,
        "actualCost": actual_cost,
    }


# ---------------------------------------------------------------------------
# Pagination
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

            except Exception as exc:
                status = getattr(getattr(exc, "response", None), "status_code", None)

                if status == 429 and retries < max_retries:
                    retry_after = None

                    if getattr(exc, "response", None) is not None:
                        retry_after = exc.response.headers.get("Retry-After")

                    wait_seconds = float(retry_after) if retry_after else min(60.0, (2 ** retries) * 5.0)
                    retries += 1

                    log.warning(
                        "429 rate limit on %s. Waiting %.1fs. Retry %d/%d",
                        label,
                        wait_seconds,
                        retries,
                        max_retries,
                    )

                    time.sleep(wait_seconds)
                    continue

                raise

        results, next_cursor = client._extract_results_and_cursor(raw)

        page += 1
        all_results.extend(results)

        log.info("[%s] page %d: %d rows, total: %d", label, page, len(results), len(all_results))

        if not next_cursor:
            break

        cursor = next_cursor
        time.sleep(page_delay)

    return all_results


# ---------------------------------------------------------------------------
# Fetch functions
# ---------------------------------------------------------------------------

def fetch_job_details(
    client: HCSSClient,
    job_ids: list[str],
) -> tuple[dict[str, str], dict[str, str], list[dict[str, Any]]]:
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
        log.warning("/jobs/advanced failed: %s", exc)
        job_list = []

    job_code_map: dict[str, str] = {}
    job_name_map: dict[str, str] = {}

    for job in job_list:
        jid = job.get("id")

        if not jid:
            continue

        code = job.get("code") or job.get("jobCode") or job.get("number") or jid[:8]
        name = job.get("description") or job.get("name") or code

        job_code_map[jid] = str(code)
        job_name_map[jid] = str(name)

    for jid in job_ids:
        job_code_map.setdefault(jid, jid[:8])
        job_name_map.setdefault(jid, "")

    log.info("Jobs resolved: %d", len(job_code_map))

    return job_code_map, job_name_map, job_list


def fetch_cost_codes(client: HCSSClient, job_ids: list[str]) -> list[dict[str, Any]]:
    if not job_ids:
        return []

    payload = {
        "jobIds": job_ids,
        "limit": 500,
    }

    rows = fetch_all_pages(
        client=client,
        method="POST",
        url=COST_CODES_URL,
        payload=payload,
        label="cost codes",
        page_delay=0.75,
    )

    log.info("Cost code records found: %d", len(rows))

    return rows


def fetch_progress_for_job_range(
    client: HCSSClient,
    job_id: str,
    start_date: str,
    end_date: str,
) -> list[dict[str, Any]]:
    payload = {
        "jobId": job_id,
        "startDate": f"{start_date}T00:00:00Z",
        "endDate": f"{end_date}T23:59:59Z",
        "limit": 500,
    }

    return fetch_all_pages(
        client=client,
        method="POST",
        url=PROGRESS_URL,
        payload=payload,
        label=f"progress {job_id[:8]}",
        page_delay=1.0,
    )


def fetch_job_costs_for_job_range(
    client: HCSSClient,
    job_id: str,
    start_date: str,
    end_date: str,
) -> list[dict[str, Any]]:
    payload = {
        "jobIds": [job_id],
        "startDate": f"{start_date}T00:00:00Z",
        "endDate": f"{end_date}T23:59:59Z",
        "limit": 500,
    }

    return fetch_all_pages(
        client=client,
        method="POST",
        url=JOB_COSTS_URL,
        payload=payload,
        label=f"job costs {job_id[:8]}",
        page_delay=1.0,
    )


# ---------------------------------------------------------------------------
# Quantity calculation
# ---------------------------------------------------------------------------

def calculate_actual_quantity_by_costcode(
    progress_rows: list[dict[str, Any]],
) -> dict[str, float]:
    sums: dict[str, float] = defaultdict(float)

    for row in progress_rows:
        cost_code_id = extract_cost_code_id(row)

        if not cost_code_id:
            continue

        sums[cost_code_id] += safe_float(row.get("quantity"))

    return sums


# ---------------------------------------------------------------------------
# Actual cost calculation
# ---------------------------------------------------------------------------

def calculate_actual_cost_by_job_costcode_from_job_costs(
    job_cost_rows_by_job: dict[str, list[dict[str, Any]]],
) -> dict[tuple[str, str], dict[str, float]]:
    """
    jobCosts/advancedRequest rows do not always return jobId.

    Because we fetch job costs one job at a time, we attach the job_id from the
    fetch loop and use it as the first part of the key.

    Correct Actual logic:
        Actual = laborCost + equipmentCost + materialCost + subcontractCost + truckingCost
    """

    results: dict[tuple[str, str], dict[str, float]] = defaultdict(
        lambda: {
            "actualCost": 0.0,
            "laborCost": 0.0,
            "equipmentCost": 0.0,
            "materialCost": 0.0,
            "subcontractCost": 0.0,
            "truckingCost": 0.0,
            "quantityFromJobCosts": 0.0,
            "laborHours": 0.0,
            "equipmentHours": 0.0,
            "rowCount": 0.0,
        }
    )

    skipped = 0

    for job_id, rows in job_cost_rows_by_job.items():
        for row in rows:
            cost_code_id = extract_cost_code_id(row)

            if not cost_code_id:
                skipped += 1
                continue

            cost_parts = get_actual_cost_from_job_cost_row(row)
            key = (job_id, cost_code_id)

            results[key]["actualCost"] += cost_parts["actualCost"]
            results[key]["laborCost"] += cost_parts["laborCost"]
            results[key]["equipmentCost"] += cost_parts["equipmentCost"]
            results[key]["materialCost"] += cost_parts["materialCost"]
            results[key]["subcontractCost"] += cost_parts["subcontractCost"]
            results[key]["truckingCost"] += cost_parts["truckingCost"]

            results[key]["quantityFromJobCosts"] += safe_float(row.get("quantity"))
            results[key]["laborHours"] += safe_float(row.get("laborHours"))
            results[key]["equipmentHours"] += safe_float(row.get("equipmentHours"))
            results[key]["rowCount"] += 1

    for key in list(results.keys()):
        for field in list(results[key].keys()):
            results[key][field] = round(results[key][field], 2)

    if skipped:
        log.warning("Skipped %d job cost rows because costCodeId was missing", skipped)

    log.info("Actual costs calculated for %d job/cost-code combinations", len(results))

    return results


# ---------------------------------------------------------------------------
# Build final projection table
# ---------------------------------------------------------------------------

def build_projection_table(
    cost_codes: list[dict[str, Any]],
    actual_qty_by_ccid: dict[str, float],
    actual_cost_by_job_ccid: dict[tuple[str, str], dict[str, float]],
    job_code_map: dict[str, str],
) -> list[dict[str, Any]]:
    table: list[dict[str, Any]] = []

    for cc in cost_codes:
        job_id = extract_job_id(cc)
        cost_code_id = cc.get("id") or extract_cost_code_id(cc)

        if not job_id or not cost_code_id:
            continue

        planned_quantity = get_planned_quantity(cc)
        budgeted_cost = get_budgeted_cost(cc)

        actual_quantity = actual_qty_by_ccid.get(cost_code_id, 0.0)

        completion_pct = 0.0
        if planned_quantity:
            completion_pct = actual_quantity / planned_quantity * 100

        expected_cost = budgeted_cost * (completion_pct / 100)

        actual_detail = actual_cost_by_job_ccid.get((job_id, cost_code_id), {})
        actual_cost = safe_float(actual_detail.get("actualCost"))

        variance = expected_cost - actual_cost

        performance_factor = None
        if actual_cost:
            performance_factor = expected_cost / actual_cost

        projected_final = None
        projected_over_under = None

        if completion_pct > 0:
            projected_final = actual_cost / (completion_pct / 100)
            projected_over_under = budgeted_cost - projected_final

        table.append(
            {
                "jobId": job_id,
                "jobCode": job_code_map.get(job_id, job_id[:8]),
                "costCodeId": cost_code_id,
                "costCode": cc.get("code") or cc.get("costCode") or cost_code_id[:8],
                "description": cc.get("description") or cc.get("name") or "",
                "unit": cc.get("unit") or cc.get("unitOfMeasure") or "",

                "budgetedQuantity": round(planned_quantity, 3),
                "quantity": round(actual_quantity, 3),
                "completionPct": round(completion_pct, 2),

                "budgetedCost": round(budgeted_cost, 2),
                "expected": round(expected_cost, 2),
                "actual": round(actual_cost, 2),
                "variance": round(variance, 2),

                "performanceFactor": round(performance_factor, 3) if performance_factor else None,
                "projectedFinal": round(projected_final, 2) if projected_final is not None else None,
                "projectedOverUnder": round(projected_over_under, 2) if projected_over_under is not None else None,

                "actualLaborCost": round(safe_float(actual_detail.get("laborCost")), 2),
                "actualEquipmentCost": round(safe_float(actual_detail.get("equipmentCost")), 2),
                "actualMaterialCost": round(safe_float(actual_detail.get("materialCost")), 2),
                "actualSubcontractCost": round(safe_float(actual_detail.get("subcontractCost")), 2),
                "actualTruckingCost": round(safe_float(actual_detail.get("truckingCost")), 2),

                "quantityFromJobCosts": round(safe_float(actual_detail.get("quantityFromJobCosts")), 3),
                "laborHours": round(safe_float(actual_detail.get("laborHours")), 2),
                "equipmentHours": round(safe_float(actual_detail.get("equipmentHours")), 2),
                "jobCostRowCount": int(safe_float(actual_detail.get("rowCount"))),
            }
        )

    table.sort(key=lambda row: (row["jobCode"], row["costCode"]))

    return table


# ---------------------------------------------------------------------------
# Debug helpers
# ---------------------------------------------------------------------------

def print_matching_row_for_portal_check(
    projection_table: list[dict[str, Any]],
    job_code: str | None,
    cost_code: str | None,
) -> None:
    if not job_code and not cost_code:
        return

    print("\n" + "=" * 120)
    print("Portal check row")
    print("=" * 120)

    found = False

    for row in projection_table:
        job_match = True
        cost_code_match = True

        if job_code:
            job_match = normalize_code(row.get("jobCode")) == normalize_code(job_code)

        if cost_code:
            cost_code_match = normalize_code(row.get("costCode")) == normalize_code(cost_code)

        if job_match and cost_code_match:
            found = True
            print(json.dumps(row, indent=2, default=str))

    if not found:
        print("No exact row found for provided portal check filters.")

    print("=" * 120)


def print_actual_debug_for_cost_code(
    job_cost_rows_by_job: dict[str, list[dict[str, Any]]],
    cost_code_id: str | None,
) -> None:
    if not cost_code_id:
        return

    print("\n" + "=" * 120)
    print(f"Raw actual-cost breakdown for costCodeId: {cost_code_id}")
    print("=" * 120)

    found = False
    grand_total = 0.0

    for job_id, rows in job_cost_rows_by_job.items():
        for row in rows:
            row_cost_code_id = extract_cost_code_id(row)

            if row_cost_code_id != cost_code_id:
                continue

            found = True
            parts = get_actual_cost_from_job_cost_row(row)
            grand_total += parts["actualCost"]

            print(
                f"jobId={job_id} "
                f"date={row.get('date')} "
                f"qty={row.get('quantity')} "
                f"labor={parts['laborCost']:.2f} "
                f"equipment={parts['equipmentCost']:.2f} "
                f"material={parts['materialCost']:.2f} "
                f"subcontract={parts['subcontractCost']:.2f} "
                f"trucking={parts['truckingCost']:.2f} "
                f"actual={parts['actualCost']:.2f}"
            )

    if not found:
        print("No raw job-cost rows found for this costCodeId.")

    print("-" * 120)
    print(f"Raw actual total = {grand_total:.2f}")
    print("=" * 120)


# ---------------------------------------------------------------------------
# Main runner
# ---------------------------------------------------------------------------

def run(
    start: str,
    end: str,
    job_id: str | None,
    job_code: str | None,
    check_cost_code: str | None,
    check_cost_code_id: str | None,
) -> dict[str, Any]:
    client = HCSSClient()
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    log.info("Step 1/6: Resolving active jobs...")

    if job_id:
        job_ids = [job_id]
        timecards_for_discovery: list[dict[str, Any]] = []
    else:
        timecards_for_discovery = client.fetch_timecards(start_date=start, end_date=end)
        job_ids = sorted(
            {
                extract_job_id(timecard)
                for timecard in timecards_for_discovery
                if extract_job_id(timecard)
            }
        )

    log.info("Job IDs discovered: %d", len(job_ids))

    log.info("Step 2/6: Resolving job codes...")
    job_code_map, job_name_map, raw_jobs = fetch_job_details(client, job_ids)

    if job_code and not job_id:
        matched_job_ids = [
            jid
            for jid, code in job_code_map.items()
            if normalize_code(code) == normalize_code(job_code)
        ]

        if matched_job_ids:
            job_ids = matched_job_ids
            log.info("Filtered to job_code=%s: %d job(s)", job_code, len(job_ids))
        else:
            log.warning("job_code=%s not found among active jobs in this range", job_code)

    log.info("Step 3/6: Fetching cost codes / budget master...")
    cost_codes = fetch_cost_codes(client, job_ids)

    raw_cost_codes_path = OUTPUT_DIR / f"raw_cost_codes_{timestamp}.json"
    raw_cost_codes_path.write_text(json.dumps(cost_codes, indent=2, default=str))

    log.info("Step 4/6: Fetching cost-code progress / quantity...")
    all_progress: list[dict[str, Any]] = []

    for idx, jid in enumerate(job_ids, start=1):
        log.info(
            "Progress %d/%d: %s (%s)",
            idx,
            len(job_ids),
            job_code_map.get(jid, jid[:8]),
            jid,
        )

        try:
            all_progress.extend(fetch_progress_for_job_range(client, jid, start, end))
        except Exception as exc:
            log.warning("Progress fetch failed for job %s: %s", jid, exc)

        if idx < len(job_ids):
            time.sleep(1.0)

    raw_progress_path = OUTPUT_DIR / f"raw_progress_{timestamp}.json"
    raw_progress_path.write_text(json.dumps(all_progress, indent=2, default=str))

    log.info("Step 5/6: Fetching HCSS job costs / actual values...")

    job_cost_rows_by_job: dict[str, list[dict[str, Any]]] = {}

    for idx, jid in enumerate(job_ids, start=1):
        log.info(
            "Job costs %d/%d: %s (%s)",
            idx,
            len(job_ids),
            job_code_map.get(jid, jid[:8]),
            jid,
        )

        try:
            rows = fetch_job_costs_for_job_range(client, jid, start, end)
            job_cost_rows_by_job[jid] = rows
        except Exception as exc:
            log.warning("Job costs fetch failed for job %s: %s", jid, exc)
            job_cost_rows_by_job[jid] = []

        if idx < len(job_ids):
            time.sleep(1.0)

    raw_job_costs_path = OUTPUT_DIR / f"raw_job_costs_{timestamp}.json"
    raw_job_costs_path.write_text(json.dumps(job_cost_rows_by_job, indent=2, default=str))

    log.info("Step 6/6: Computing projection table...")

    actual_qty_by_ccid = calculate_actual_quantity_by_costcode(all_progress)
    actual_cost_by_job_ccid = calculate_actual_cost_by_job_costcode_from_job_costs(job_cost_rows_by_job)

    projection_table = build_projection_table(
        cost_codes=cost_codes,
        actual_qty_by_ccid=actual_qty_by_ccid,
        actual_cost_by_job_ccid=actual_cost_by_job_ccid,
        job_code_map=job_code_map,
    )

    output = {
        "dateRange": {
            "start": start,
            "end": end,
        },
        "generatedAt": datetime.now().isoformat(),
        "filters": {
            "jobId": job_id,
            "jobCode": job_code,
            "checkCostCode": check_cost_code,
            "checkCostCodeId": check_cost_code_id,
        },
        "apiSources": {
            "jobsAdvanced": f"{client.heavyjob_base}/jobs/advanced",
            "costCodes": COST_CODES_URL,
            "progress": PROGRESS_URL,
            "jobCosts": JOB_COSTS_URL,
        },
        "calculationRule": {
            "budgetedQuantity": "costCodes/advancedRequest field 'quantity'",
            "quantity": "SUM costCode/progress/advancedRequest field 'quantity' within selected date range",
            "completionPct": "quantity / budgetedQuantity * 100",
            "budgetedCost": "laborDollars + equipmentDollars + materialDollars + subcontractDollars",
            "expected": "budgetedCost * completionPct",
            "actual": "SUM jobCosts/advancedRequest fields: laborCost + equipmentCost + materialCost + subcontractCost + truckingCost",
            "variance": "expected - actual",
            "performanceFactor": "expected / actual",
            "projectedFinal": "actual / completionPct",
            "projectedOverUnder": "budgetedCost - projectedFinal",
        },
        "summary": {
            "jobsSelected": len(job_ids),
            "costCodesCount": len(cost_codes),
            "progressRecordsCount": len(all_progress),
            "jobCostJobsCount": len(job_cost_rows_by_job),
            "jobCostRecordsCount": sum(len(rows) for rows in job_cost_rows_by_job.values()),
            "rowsCount": len(projection_table),
            "rawCostCodesFile": str(raw_cost_codes_path),
            "rawProgressFile": str(raw_progress_path),
            "rawJobCostsFile": str(raw_job_costs_path),
        },
        "data": projection_table,
    }

    computed_path = OUTPUT_DIR / f"computed_projection_{timestamp}.json"
    computed_path.write_text(json.dumps(output, indent=2, default=str))

    log.info("Done. Saved computed output: %s", computed_path.resolve())
    log.info("Raw job costs saved here: %s", raw_job_costs_path.resolve())

    print("\n" + "=" * 140)
    print(f"Date range: {start} -> {end}")
    print(f"Rows: {len(projection_table)}")
    print("=" * 140)

    for row in projection_table[:40]:
        print(
            f"Job {row['jobCode']:<10} "
            f"CC {row['costCode']:<12} "
            f"{row['description'][:30]:<30} "
            f"BudgetQty={row['budgetedQuantity']:<12} "
            f"Qty={row['quantity']:<12} "
            f"Compl%={row['completionPct']:<8} "
            f"Budget=${row['budgetedCost']:<12} "
            f"Expected=${row['expected']:<12} "
            f"Actual=${row['actual']:<12} "
            f"Variance=${row['variance']:<12}"
        )

    print("=" * 140)
    print(f"Saved computed file: {computed_path.resolve()}")
    print(f"Saved raw job costs file: {raw_job_costs_path.resolve()}")

    print_matching_row_for_portal_check(
        projection_table=projection_table,
        job_code=job_code,
        cost_code=check_cost_code,
    )

    print_actual_debug_for_cost_code(
        job_cost_rows_by_job=job_cost_rows_by_job,
        cost_code_id=check_cost_code_id,
    )

    return output


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Fetch and compute HCSS HeavyJob Production Analysis values for verification"
    )

    parser.add_argument("--start", required=True, help="Start date YYYY-MM-DD")
    parser.add_argument("--end", required=True, help="End date YYYY-MM-DD")
    parser.add_argument("--job-id", default=None, help="Exact HCSS job UUID")
    parser.add_argument("--job-code", default=None, help="Human job code, example: 25006")
    parser.add_argument("--check-cost-code", default=None, help="Optional cost code to print exact row, example: 22711111")
    parser.add_argument("--check-cost-code-id", default=None, help="Optional cost code UUID to print raw actual breakdown")

    args = parser.parse_args()

    for label, value in [("--start", args.start), ("--end", args.end)]:
        try:
            datetime.strptime(value, "%Y-%m-%d")
        except ValueError:
            print(f"ERROR: invalid date for {label}: {value}. Expected YYYY-MM-DD.")
            sys.exit(1)

    run(
        start=args.start,
        end=args.end,
        job_id=args.job_id,
        job_code=args.job_code,
        check_cost_code=args.check_cost_code,
        check_cost_code_id=args.check_cost_code_id,
    )


if __name__ == "__main__":
    main()