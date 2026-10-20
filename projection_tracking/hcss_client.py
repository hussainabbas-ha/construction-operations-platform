"""
projection_tracking/hcss_client.py

HCSS API wrapper for Projection / Production Analysis data.
"""

from __future__ import annotations

import calendar
import logging
import time
from collections import defaultdict
from typing import Any

from payroll_verification.hcss_client import HCSSClient

logger = logging.getLogger(__name__)

COST_CODES_URL = "https://api.hcssapps.com/heavyjob/api/v2/costCodes/advancedRequest"
PROGRESS_URL = "https://api.hcssapps.com/heavyjob/api/v1/costCode/progress/advancedRequest"
JOB_COSTS_URL = "https://api.hcssapps.com/heavyjob/api/v1/jobCosts/advancedRequest"

BUDGET_DOLLAR_FIELDS = [
    "laborDollars",
    "equipmentDollars",
    "materialDollars",
    "subcontractDollars",
]


class ProjectionHCSSClient:
    """Wrapper for HCSS projection-specific API calls."""

    def __init__(self, client: HCSSClient | None = None) -> None:
        self.client = client or HCSSClient()

    def fetch_projection_data(
        self,
        *,
        month: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> list[dict[str, Any]]:
        """
        Fetch Production Analysis rows.

        Supports:
            month="YYYY-MM"
            or
            start_date="YYYY-MM-DD", end_date="YYYY-MM-DD"
        """
        if start_date and end_date:
            period_start, period_end = start_date, end_date
        elif month:
            period_start, period_end = self._month_to_date_range(month)
        else:
            raise ValueError("Either month or start_date/end_date is required.")

        logger.info("Fetching projection data: %s -> %s", period_start, period_end)

        job_ids, job_map = self._discover_active_jobs(period_start, period_end)
        if not job_ids:
            logger.warning("No active jobs found for projection period=%s -> %s", period_start, period_end)
            return []

        job_details = self._fetch_job_details(job_ids)
        cost_codes = self._fetch_cost_codes(job_ids)

        progress_rows: list[dict[str, Any]] = []
        for idx, jid in enumerate(job_ids, start=1):
            logger.info("Fetching progress %d/%d job=%s", idx, len(job_ids), jid)
            try:
                progress_rows.extend(self._fetch_progress_for_job_range(jid, period_start, period_end))
            except Exception as exc:
                logger.warning("Progress fetch failed for job=%s: %s", jid, exc)
            if idx < len(job_ids):
                time.sleep(1.0)

        job_cost_rows_by_job: dict[str, list[dict[str, Any]]] = {}
        for idx, jid in enumerate(job_ids, start=1):
            logger.info("Fetching job costs %d/%d job=%s", idx, len(job_ids), jid)
            try:
                job_cost_rows_by_job[jid] = self._fetch_job_costs_for_job_range(jid, period_start, period_end)
            except Exception as exc:
                logger.warning("Job costs fetch failed for job=%s: %s", jid, exc)
                job_cost_rows_by_job[jid] = []
            if idx < len(job_ids):
                time.sleep(1.0)

        actual_qty_by_ccid = self._calculate_actual_quantity_by_costcode(progress_rows)
        actual_cost_by_job_ccid = self._calculate_actual_cost_by_job_costcode_from_job_costs(job_cost_rows_by_job)

        rows = self._build_projection_rows(
            cost_codes=cost_codes,
            actual_qty_by_ccid=actual_qty_by_ccid,
            actual_cost_by_job_ccid=actual_cost_by_job_ccid,
            job_map=job_map,
            job_details=job_details,
            period_start=period_start,
            period_end=period_end,
        )

        logger.info("Projection data assembled: %s -> %s rows=%d", period_start, period_end, len(rows))
        return rows

    @staticmethod
    def _safe_float(value: Any, default: float = 0.0) -> float:
        try:
            return float(value or 0)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _month_to_date_range(month: str) -> tuple[str, str]:
        year, mon = int(month[:4]), int(month[5:7])
        last_day = calendar.monthrange(year, mon)[1]
        return f"{month}-01", f"{month}-{last_day:02d}"

    @staticmethod
    def _extract_job_id(row: dict[str, Any]) -> str:
        return row.get("jobId") or row.get("job_id") or row.get("job", {}).get("id") or ""

    @staticmethod
    def _extract_cost_code_id(row: dict[str, Any]) -> str:
        return (
            row.get("costCodeId")
            or row.get("cost_code_id")
            or row.get("id")
            or row.get("costCode", {}).get("id")
            or row.get("costCode", {}).get("costCodeId")
            or ""
        )

    def _get_planned_quantity(self, cost_code: dict[str, Any]) -> float:
        return self._safe_float(
            cost_code.get("quantity")
            or cost_code.get("plannedQuantity")
            or cost_code.get("budgetQuantity")
            or cost_code.get("budgetedQuantity")
        )

    def _get_budgeted_cost(self, cost_code: dict[str, Any]) -> float:
        return sum(self._safe_float(cost_code.get(field)) for field in BUDGET_DOLLAR_FIELDS)

    def _get_actual_cost_from_job_cost_row(self, row: dict[str, Any]) -> dict[str, float]:
        labor_cost = self._safe_float(row.get("laborCost"))
        equipment_cost = self._safe_float(row.get("equipmentCost"))
        material_cost = self._safe_float(row.get("materialCost"))
        subcontract_cost = self._safe_float(row.get("subcontractCost"))
        trucking_cost = self._safe_float(row.get("truckingCost"))
        actual_cost = labor_cost + equipment_cost + material_cost + subcontract_cost + trucking_cost
        return {
            "laborCost": labor_cost,
            "equipmentCost": equipment_cost,
            "materialCost": material_cost,
            "subcontractCost": subcontract_cost,
            "truckingCost": trucking_cost,
            "actualCost": actual_cost,
        }

    def _fetch_all_pages(
        self,
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
                        raw = self.client._request(method, url, json=req_payload)
                    else:
                        raw = self.client._request(method, url, params=req_payload)
                    break
                except Exception as exc:
                    status = getattr(getattr(exc, "response", None), "status_code", None)
                    if status == 429 and retries < max_retries:
                        retry_after = None
                        if getattr(exc, "response", None) is not None:
                            retry_after = exc.response.headers.get("Retry-After")
                        wait_seconds = float(retry_after) if retry_after else min(60.0, (2 ** retries) * 5.0)
                        retries += 1
                        logger.warning(
                            "429 rate limit on %s. Waiting %.1fs. Retry %d/%d",
                            label,
                            wait_seconds,
                            retries,
                            max_retries,
                        )
                        time.sleep(wait_seconds)
                        continue
                    raise

            results, next_cursor = self.client._extract_results_and_cursor(raw)
            page += 1
            all_results.extend(results)
            logger.info("[%s] page %d: %d rows total=%d", label, page, len(results), len(all_results))

            if not next_cursor:
                break

            cursor = next_cursor
            time.sleep(page_delay)

        return all_results

    def _discover_active_jobs(self, start_date: str, end_date: str) -> tuple[list[str], dict[str, str]]:
        try:
            timecards = self.client.fetch_timecards(start_date=start_date, end_date=end_date)
        except Exception as exc:
            raise RuntimeError(f"Failed to fetch timecards for projection: {exc}") from exc

        job_map: dict[str, str] = {}
        for tc in timecards:
            jid = tc.get("jobId") or tc.get("job_id")
            jcode = tc.get("jobCode") or tc.get("job_code") or tc.get("jobDescription") or "Unknown"
            if jid and jid not in job_map:
                job_map[jid] = str(jcode)

        logger.info("Discovered %d active jobs from timecards (%s -> %s)", len(job_map), start_date, end_date)
        return list(job_map.keys()), job_map

    def _fetch_job_details(self, job_ids: list[str]) -> dict[str, dict[str, Any]]:
        if not job_ids:
            return {}

        try:
            raw = self.client._request(
                "POST",
                f"{self.client.heavyjob_base}/jobs/advanced",
                json={"jobIds": job_ids},
            )
            job_list = raw if isinstance(raw, list) else raw.get("results", [])
        except Exception as exc:
            logger.error("Failed to fetch job details: %s", exc)
            return {}

        details: dict[str, dict[str, Any]] = {}
        for job in job_list:
            jid = job.get("id")
            if jid:
                details[jid] = job

        logger.info("Fetched details for %d/%d jobs", len(details), len(job_ids))
        return details

    def _fetch_cost_codes(self, job_ids: list[str]) -> list[dict[str, Any]]:
        if not job_ids:
            return []
        rows = self._fetch_all_pages(
            "POST",
            COST_CODES_URL,
            {"jobIds": job_ids, "limit": 500},
            label="cost codes",
            page_delay=0.75,
        )
        logger.info("Fetched cost code records: %d", len(rows))
        return rows

    def _fetch_progress_for_job_range(self, job_id: str, start_date: str, end_date: str) -> list[dict[str, Any]]:
        return self._fetch_all_pages(
            "POST",
            PROGRESS_URL,
            {
                "jobId": job_id,
                "startDate": f"{start_date}T00:00:00Z",
                "endDate": f"{end_date}T23:59:59Z",
                "limit": 500,
            },
            label=f"progress {job_id[:8]}",
            page_delay=1.0,
        )

    def _fetch_job_costs_for_job_range(self, job_id: str, start_date: str, end_date: str) -> list[dict[str, Any]]:
        return self._fetch_all_pages(
            "POST",
            JOB_COSTS_URL,
            {
                "jobIds": [job_id],
                "startDate": f"{start_date}T00:00:00Z",
                "endDate": f"{end_date}T23:59:59Z",
                "limit": 500,
            },
            label=f"job costs {job_id[:8]}",
            page_delay=1.0,
        )

    def _calculate_actual_quantity_by_costcode(self, progress_rows: list[dict[str, Any]]) -> dict[str, float]:
        sums: dict[str, float] = defaultdict(float)
        for row in progress_rows:
            cost_code_id = self._extract_cost_code_id(row)
            if not cost_code_id:
                continue
            sums[cost_code_id] += self._safe_float(row.get("quantity"))
        return sums

    def _calculate_actual_cost_by_job_costcode_from_job_costs(
        self,
        job_cost_rows_by_job: dict[str, list[dict[str, Any]]],
    ) -> dict[tuple[str, str], dict[str, float]]:
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
                cost_code_id = self._extract_cost_code_id(row)
                if not cost_code_id:
                    skipped += 1
                    continue

                cost_parts = self._get_actual_cost_from_job_cost_row(row)
                key = (job_id, cost_code_id)

                results[key]["actualCost"] += cost_parts["actualCost"]
                results[key]["laborCost"] += cost_parts["laborCost"]
                results[key]["equipmentCost"] += cost_parts["equipmentCost"]
                results[key]["materialCost"] += cost_parts["materialCost"]
                results[key]["subcontractCost"] += cost_parts["subcontractCost"]
                results[key]["truckingCost"] += cost_parts["truckingCost"]
                results[key]["quantityFromJobCosts"] += self._safe_float(row.get("quantity"))
                results[key]["laborHours"] += self._safe_float(row.get("laborHours"))
                results[key]["equipmentHours"] += self._safe_float(row.get("equipmentHours"))
                results[key]["rowCount"] += 1

        for key in list(results.keys()):
            for field in list(results[key].keys()):
                results[key][field] = round(results[key][field], 2)

        if skipped:
            logger.warning("Skipped %d job cost rows because costCodeId was missing", skipped)

        logger.info("Actual costs calculated for %d job/cost-code combinations", len(results))
        return results

    def _build_projection_rows(
        self,
        *,
        cost_codes: list[dict[str, Any]],
        actual_qty_by_ccid: dict[str, float],
        actual_cost_by_job_ccid: dict[tuple[str, str], dict[str, float]],
        job_map: dict[str, str],
        job_details: dict[str, dict[str, Any]],
        period_start: str,
        period_end: str,
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []

        for cc in cost_codes:
            job_id = self._extract_job_id(cc)
            cost_code_id = cc.get("id") or self._extract_cost_code_id(cc)

            if not job_id or not cost_code_id:
                continue

            detail = job_details.get(job_id, {})
            job_code = detail.get("code") or detail.get("jobCode") or job_map.get(job_id, "Unknown")
            job_name = detail.get("description") or detail.get("name") or job_code
            business_unit = detail.get("businessUnitId") or cc.get("businessUnitId") or cc.get("businessUnitCode") or "N/A"

            budgeted_quantity = self._get_planned_quantity(cc)
            budgeted_cost = self._get_budgeted_cost(cc)
            quantity = actual_qty_by_ccid.get(cost_code_id, 0.0)

            completion_pct = 0.0
            if budgeted_quantity:
                completion_pct = quantity / budgeted_quantity * 100

            expected = budgeted_cost * (completion_pct / 100.0)

            actual_detail = actual_cost_by_job_ccid.get((job_id, cost_code_id), {})
            actual = self._safe_float(actual_detail.get("actualCost"))
            variance = expected - actual

            performance_factor = 0.0
            if actual:
                performance_factor = expected / actual

            projected_final = 0.0
            projected_over_under = 0.0
            if completion_pct > 0:
                projected_final = actual / (completion_pct / 100.0)
                projected_over_under = budgeted_cost - projected_final

            rows.append(
                {
                    "period_start": period_start,
                    "period_end": period_end,
                    "job_id": job_id,
                    "job_code": str(job_code),
                    "job_name": str(job_name),
                    "business_unit": str(business_unit),
                    "cost_code_id": cost_code_id,
                    "cost_code": cc.get("code") or cc.get("costCode") or cost_code_id[:8],
                    "cost_code_description": cc.get("description") or cc.get("name") or "",
                    "unit": cc.get("unit") or cc.get("unitOfMeasure") or "",
                    "budgeted_quantity": round(budgeted_quantity, 3),
                    "quantity": round(quantity, 3),
                    "completion_pct": round(completion_pct, 2),
                    "budgeted_cost": round(budgeted_cost, 2),
                    "expected": round(expected, 2),
                    "actual": round(actual, 2),
                    "variance": round(variance, 2),
                    "projected_final": round(projected_final, 2),
                    "projected_over_under": round(projected_over_under, 2),
                    "performance_factor": round(performance_factor, 3),
                    "actual_labor_cost": round(self._safe_float(actual_detail.get("laborCost")), 2),
                    "actual_equipment_cost": round(self._safe_float(actual_detail.get("equipmentCost")), 2),
                    "actual_material_cost": round(self._safe_float(actual_detail.get("materialCost")), 2),
                    "actual_subcontract_cost": round(self._safe_float(actual_detail.get("subcontractCost")), 2),
                    "actual_trucking_cost": round(self._safe_float(actual_detail.get("truckingCost")), 2),
                    "quantity_from_job_costs": round(self._safe_float(actual_detail.get("quantityFromJobCosts")), 3),
                    "labor_hours": round(self._safe_float(actual_detail.get("laborHours")), 2),
                    "equipment_hours": round(self._safe_float(actual_detail.get("equipmentHours")), 2),
                }
            )

        rows.sort(key=lambda row: (row["job_code"], row["cost_code"]))
        return rows