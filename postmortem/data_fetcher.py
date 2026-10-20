"""
postmortem/data_fetcher.py
───────────────────────────
Fetches and computes postmortem data for all active HCSS jobs.

This module ports the verified formula from scripts/get_job_expected_all_cost.py
into a reusable callable for the cron job and backfill script.

Formula (matches HCSS portal Cost Report — verified on job 25006):

    Per cost code:
        T&M  (isTm=True)           : expected = actual
        Normal with qty + budget   : formula  = (actualQty / budgetQty) * budgetedAllCost
                                     if isCapExpected (default True):
                                         expected = min(formula, budgetedAllCost)
                                     else:
                                         expected = formula
        No qty or no budget        : expected = 0

    Variance = expected - actual  (positive = under budget)

Data sources (2 HCSS API calls per job):
    GET /v1/costCodes?jobId=...     — budget data (qty, dollar budgets, isTm, isCapExpected)
    GET /v1/jobs/{jobId}/costs      — actual cost + production qty (single authoritative call)

Job list:
    GET /v1/jobs                    — full list of active jobs (paginated)

HCSS docs:
    Expected formula  : https://help.hcss.com/s/article/How-is-Expected-Actual-and-Variance-calculated
    Cap Expected flag : https://help.hcss.com/s/article/How-to-Set-Cost-Codes-to-Cap-Expected-in-HeavyJob-Web-Only
"""
from __future__ import annotations

import logging
import time
from typing import Any

logger = logging.getLogger(__name__)

_BASE          = "https://api.hcssapps.com/heavyjob/api"
_JOBS_LIST     = f"{_BASE}/v1/jobs"
_COST_CODES_V1 = f"{_BASE}/v1/costCodes"
_JOB_COSTS     = f"{_BASE}/v1/jobs"          # GET {base}/{jobId}/costs

# Budget dollar fields to sum for budgetedAllCost (mirrors the script exactly)
_BUDGET_FIELDS = [
    "laborDollars",
    "equipmentDollars",
    "materialDollars",
    "subcontractDollars",
    "supplyDollars",
]


# ── Private helpers ───────────────────────────────────────────────────────────

def _sf(val: Any) -> float:
    """Safe coerce to float; returns 0.0 on None / invalid."""
    try:
        return float(val or 0)
    except (TypeError, ValueError):
        return 0.0


def _util_pct(actual: float, expected: float) -> float | None:
    """Return (actual / expected) * 100, or None when expected == 0."""
    if expected <= 0:
        return None
    return round((actual / expected) * 100, 2)


def _with_retry(fn, *, label: str = "", max_retries: int = 8) -> Any:
    """Execute fn(); retry on HTTP 429 with exponential back-off."""
    import requests as _req

    retries = 0
    while True:
        try:
            return fn()
        except _req.exceptions.HTTPError as exc:
            code = exc.response.status_code if exc.response is not None else None
            if code == 429 and retries < max_retries:
                ra   = exc.response.headers.get("Retry-After") if exc.response else None
                wait = float(ra) if ra else min(120.0, (2 ** retries) * 5.0)
                retries += 1
                logger.warning(
                    "[429] %s — waiting %.0fs (retry %d/%d)",
                    label, wait, retries, max_retries,
                )
                time.sleep(wait)
                continue
            raise


def _get_pages(
    client: Any,
    url: str,
    params: dict,
    *,
    label: str,
    delay: float = 0.4,
) -> list[dict]:
    """Paginate through a GET endpoint using cursor-based pagination."""
    rows: list[dict] = []
    cursor: str | None = None

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
    """
    Sum all budget dollar fields on a v1/costCodes row, including custom types.
    Mirrors _budgeted_all_cost() in get_job_expected_all_cost.py exactly.
    """
    total  = sum(_sf(cc.get(f)) for f in _BUDGET_FIELDS)
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


def _parse_job_costs_response(
    raw: dict[str, Any],
) -> tuple[dict[str, float], dict[str, float], float]:
    """
    Parse GET /jobs/{id}/costs into (actual_map, qty_map, totalCost).

    actual_map  : cost_code_id → total actual dollars (all cost types)
    qty_map     : cost_code_id → cumulative production qty
    totalCost   : job-level total from HCSS (matches Job Summary / Cost Report)

    NOTE: actual_map sums laborCost + equipmentCost + materialCost +
          subcontractCost + truckingCost + customCostTypeValues[].cost
          This is identical to what get_job_expected_all_cost.py does.
    """
    cc_rows    = raw.get("costCodeCosts") or []
    total_cost = round(_sf(raw.get("totalCost")), 2)
    actual_map: dict[str, float] = {}
    qty_map:    dict[str, float] = {}

    for row in cc_rows:
        ccid = row.get("costCodeId") or ""
        if not ccid:
            continue

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
        actual_map[ccid] = round(base + custom, 2)
        qty_map[ccid]    = _sf(row.get("quantity"))

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
    Compute Expected All Cost for one cost code.

    Returns (expected_all_cost, calc_method, raw_formula_or_none).

    This is a direct port of _compute_expected_for_cc() from
    get_job_expected_all_cost.py — the logic is intentionally identical.
    """
    if is_tm:
        return actual_cost, "T&M (expected = actual)", None

    if budgeted_qty > 0 and budgeted_cost > 0:
        formula = budgeted_cost * (actual_qty / budgeted_qty)
        if is_cap_expected:
            capped = min(formula, budgeted_cost)
            method = (
                "cap-at-budget (qty > 100%)"
                if capped < formula - 0.005
                else "formula"
            )
            return capped, method, formula
        return formula, "formula (no cap)", formula

    return 0.0, "zero (no qty/budget)", None


# ── Per-job computation ───────────────────────────────────────────────────────

def compute_job_postmortem(
    cost_codes: list[dict[str, Any]],
    actual_map: dict[str, float],
    qty_map:    dict[str, float],
    job_total_cost: float,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """
    Run the Expected All Cost formula over all cost codes for one job.

    Args:
        cost_codes     : rows from GET /v1/costCodes (budget data + flags)
        actual_map     : cost_code_id → actual cost from GET /jobs/{id}/costs
        qty_map        : cost_code_id → production qty from GET /jobs/{id}/costs
        job_total_cost : authoritative job total from GET /jobs/{id}/costs totalCost field

    Returns:
        (totals, cc_rows)

        totals  — job-level dict:
            budgeted_all_cost, expected_all_cost, actual_cost, variance,
            utilization_pct, capped_cost_code_count

        cc_rows — per-cost-code list, sorted by cost_code, each dict:
            cost_code_id, cost_code, description,
            is_tm, is_cap_expected,
            budgeted_qty, actual_qty, completion_pct,
            budgeted_all_cost, raw_formula, expected_all_cost,
            actual_cost, variance, calc_method,
            utilization_pct, loss_amount, loss_pct
    """
    rows: list[dict[str, Any]] = []
    total_expected = 0.0
    total_budgeted = 0.0
    seen_ccids: set[str] = set()

    for cc in cost_codes:
        ccid = cc.get("id") or ""
        if not ccid:
            continue
        seen_ccids.add(ccid)

        is_tm  = bool(cc.get("isTm"))
        is_cap = bool(cc.get("isCapExpected", True))
        bq     = _sf(cc.get("quantity"))
        bc     = _budgeted_all_cost(cc)
        aq     = qty_map.get(ccid, 0.0)
        ac     = actual_map.get(ccid, 0.0)

        total_budgeted += bc

        expected, method, raw_formula = _compute_expected_for_cc(
            is_tm=is_tm,
            is_cap_expected=is_cap,
            budgeted_qty=bq,
            budgeted_cost=bc,
            actual_qty=aq,
            actual_cost=ac,
        )
        total_expected += expected

        variance    = round(expected - ac, 2)
        util        = _util_pct(ac, expected)
        loss        = round(max(0.0, ac - expected), 2)
        loss_pct    = round((loss / expected) * 100, 2) if expected > 0 else None
        compl_pct   = round(aq / bq * 100, 2) if bq > 0 else None

        rows.append({
            "cost_code_id":     ccid,
            "cost_code":        cc.get("code") or "",
            "description":      cc.get("description") or "",
            "is_tm":            is_tm,
            "is_cap_expected":  is_cap,
            "budgeted_qty":     round(bq, 4),
            "actual_qty":       round(aq, 4),
            "completion_pct":   compl_pct,
            "budgeted_all_cost": round(bc, 2),
            "raw_formula":      round(raw_formula, 2) if raw_formula is not None else None,
            "expected_all_cost": round(expected, 2),
            "actual_cost":      round(ac, 2),
            "variance":         variance,
            "calc_method":      method,
            "utilization_pct":  util,
            "loss_amount":      loss,
            "loss_pct":         loss_pct,
        })

    # ── Unbudgeted cost codes ─────────────────────────────────────────────
    # These appear in actuals (actual_map) but have no corresponding budget row.
    # expected = 0, entire actual is a loss.
    for ccid, ac in actual_map.items():
        if ccid in seen_ccids or ac == 0:
            continue
        loss = round(ac, 2)
        rows.append({
            "cost_code_id":     ccid,
            "cost_code":        "",
            "description":      "(unbudgeted)",
            "is_tm":            False,
            "is_cap_expected":  False,
            "budgeted_qty":     0.0,
            "actual_qty":       round(qty_map.get(ccid, 0.0), 4),
            "completion_pct":   None,
            "budgeted_all_cost": 0.0,
            "raw_formula":      None,
            "expected_all_cost": 0.0,
            "actual_cost":      round(ac, 2),
            "variance":         round(-ac, 2),
            "calc_method":      "unbudgeted",
            "utilization_pct":  None,
            "loss_amount":      loss,
            "loss_pct":         None,
        })

    rows.sort(key=lambda r: (r["cost_code"] or "zzz", r["cost_code_id"]))

    # ── Job-level totals ──────────────────────────────────────────────────
    # Use the authoritative totalCost from GET /jobs/{id}/costs for actual_cost.
    # This matches the HCSS Job Summary report exactly.
    total_actual   = round(job_total_cost, 2)
    total_variance = round(total_expected - total_actual, 2)
    util_pct       = _util_pct(total_actual, total_expected)
    capped_count   = sum(
        1 for r in rows if r["calc_method"] == "cap-at-budget (qty > 100%)"
    )

    totals: dict[str, Any] = {
        "budgeted_all_cost":      round(total_budgeted, 2),
        "expected_all_cost":      round(total_expected, 2),
        "actual_cost":            total_actual,
        "variance":               total_variance,
        "utilization_pct":        util_pct,
        "capped_cost_code_count": capped_count,
    }
    return totals, rows


# ── Main public entry point ───────────────────────────────────────────────────

def fetch_all_jobs_postmortem(
    *,
    inter_job_delay: float = 0.5,
    page_delay:      float = 0.4,
) -> list[dict[str, Any]]:
    """
    Fetch and compute postmortem data for every active job in HCSS.

    Makes 2 HCSS API calls per job:
        1. GET /v1/costCodes?jobId=...  — budget data + isTm / isCapExpected flags
        2. GET /v1/jobs/{jobId}/costs   — actual cost + production qty

    Plus one call to seed the job list:
        GET /v1/jobs (paginated)

    Returns:
        List of job dicts, each containing:
            job_id        : str   — HCSS UUID
            job_code      : str   — human-readable code e.g. "25006"
            job_name      : str   — job description
            business_unit : str | None
            totals        : dict  — job-level KPIs (see compute_job_postmortem)
            cost_codes    : list  — per-cost-code detail rows

    Individual job errors are logged and skipped — partial results are returned
    so a single bad job never blocks the whole batch.
    """
    # Import here so the module can be imported without triggering .env load
    from payroll_verification.hcss_client import HCSSClient

    client = HCSSClient()

    # ── 1. Fetch full job list ─────────────────────────────────────────────
    logger.info("[postmortem] Fetching job list from HCSS ...")
    try:
        job_rows = _get_pages(
            client, _JOBS_LIST, {"limit": 500},
            label="jobs list", delay=page_delay,
        )
    except Exception as exc:
        logger.error("[postmortem] Failed to fetch job list: %s", exc)
        raise

    if not job_rows:
        logger.warning("[postmortem] HCSS returned 0 jobs — nothing to process")
        return []

    logger.info("[postmortem] %d total job(s) from HCSS", len(job_rows))

    # ── 2. Filter to active jobs only ─────────────────────────────────────
    # GET /v1/jobs returns all jobs regardless of status. The "status" field
    # on each job row is one of: "active", "inactive", "completed".
    # We also exclude soft-deleted jobs (isDeleted=true).
    # This matches what the HCSS portal shows under "Active Jobs" (45 jobs).
    active_jobs = [
        j for j in job_rows
        if str(j.get("status") or "").lower() == "active"
        and not j.get("isDeleted", False)
    ]

    logger.info(
        "[postmortem] %d active job(s) after filtering (skipped %d inactive/completed/deleted)",
        len(active_jobs),
        len(job_rows) - len(active_jobs),
    )

    if not active_jobs:
        logger.warning("[postmortem] No active jobs found — nothing to process")
        return []

    # ── 3. Process each active job ─────────────────────────────────────────
    results: list[dict[str, Any]] = []

    for idx, job in enumerate(active_jobs, 1):
        job_id   = (job.get("id") or "").strip()
        job_code = str(job.get("code") or job.get("jobCode") or "").strip()
        job_name = str(job.get("description") or job.get("name") or "").strip()
        biz_unit = (
            str(job.get("businessUnit") or job.get("business_unit") or "").strip() or None
        )

        if not job_id:
            logger.warning("[postmortem] Skipping job entry with no id: %s", job)
            continue

        label = job_code or job_id[:8]
        logger.info(
            "[postmortem] [%d/%d] %s — %s",
            idx, len(active_jobs), label, job_name,
        )

        try:
            # 2a. GET /v1/costCodes — budget data, flags, planned qty
            cost_codes = _get_pages(
                client,
                _COST_CODES_V1,
                {"jobId": job_id, "limit": 500},
                label=f"costCodes/{label}",
                delay=page_delay,
            )

            if not cost_codes:
                # Job has no cost codes in HCSS — nothing to compute
                logger.debug("[postmortem] %s: 0 cost codes — skipping", label)
                continue

            time.sleep(inter_job_delay)

            # 2b. GET /v1/jobs/{id}/costs — actual cost + production qty
            raw_costs = _with_retry(
                lambda jid=job_id: client._request(
                    "GET", f"{_JOB_COSTS}/{jid}/costs"
                ),
                label=f"jobs/{label}/costs",
            )
            actual_map, qty_map, job_total_cost = _parse_job_costs_response(raw_costs)

            # 2c. Run the Expected All Cost formula
            totals, cc_rows = compute_job_postmortem(
                cost_codes, actual_map, qty_map, job_total_cost,
            )

        except Exception as exc:
            logger.error(
                "[postmortem] Error processing job %s (%s): %s",
                label, job_id, exc,
                exc_info=True,
            )
            continue  # skip this job; do not abort the rest

        results.append({
            "job_id":        job_id,
            "job_code":      job_code,
            "job_name":      job_name,
            "business_unit": biz_unit,
            "totals":        totals,
            "cost_codes":    cc_rows,
        })

        # Polite pause between jobs to respect HCSS rate limits
        if idx < len(active_jobs):
            time.sleep(inter_job_delay)

    logger.info(
        "[postmortem] Done: %d/%d jobs processed successfully",
        len(results), len(active_jobs),
    )
    return results
