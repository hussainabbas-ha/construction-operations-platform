"""
Billing Draft builder.

Aggregates a calendar month of budget + quantity data into flat per-cost-code
rows (one row per job_code + cost_code), matching the Budget grid exactly.

It REUSES the Budget module's own pipeline (api.routes.budget) so every value —
budgeted_all_cost, expected_all_cost, variance, quantities — is identical to what
the Budget module shows and therefore to HCSS. No new HCSS APIs are introduced;
the daily cron already keeps the underlying budget/quantity data fresh, and the
cache-aside loaders fetch any missing day from HCSS on demand.

Expected cost:   expected_all_cost = (actual_qty / planned_qty) * budgeted_all_cost
Billing cap:     capped_expected_all_cost = min(expected_all_cost, budgeted_all_cost)
"""
from __future__ import annotations

import calendar
import logging

logger = logging.getLogger(__name__)


def month_bounds(period: str) -> tuple[str, str]:
    """'YYYY-MM' -> ('YYYY-MM-01', 'YYYY-MM-<last>')."""
    year, month = (int(x) for x in period.split("-"))
    last_day = calendar.monthrange(year, month)[1]
    return f"{year:04d}-{month:02d}-01", f"{year:04d}-{month:02d}-{last_day:02d}"


def build_billing_draft_rows(period: str) -> list[dict]:
    """Build flat billing-draft rows for the month *period* ('YYYY-MM').

    Returns one dict per (job_code, cost_code). Values are aggregated across the
    whole month and computed via the Budget module's functions, so they match the
    Budget grid / HCSS with no divergence.
    """
    # Imported lazily so importing this module never drags in FastAPI/DB at import time.
    from api.database import SessionLocal
    from api.db_services import get_or_fetch_budget_for_date
    from api.routes.budget import _aggregate_budget, _fetch_quantity_lookup, _date_list

    start_date, end_date = month_bounds(period)
    logger.info("Building billing draft for %s (%s..%s)", period, start_date, end_date)

    db = SessionLocal()
    raw: list[dict] = []
    try:
        for day in _date_list(start_date, end_date):
            raw.extend(get_or_fetch_budget_for_date(db, day))
    finally:
        db.close()

    # Same quantity join + aggregation + expected_budget/metrics as the Budget grid
    qty_lookup = _fetch_quantity_lookup(start_date, end_date)
    aggregated = _aggregate_budget(raw, qty_lookup)

    rows: list[dict] = []
    for i in aggregated:
        cost_code = (i.get("cost_code") or "").strip()
        if not cost_code:
            continue

        job_name = i.get("job_name") or ""
        if " - " in job_name:                       # 'CODE - Description'
            job_code, job_description = job_name.split(" - ", 1)
        else:
            job_code, job_description = job_name, ""

        budgeted = float(i.get("budgeted_all_cost") or 0.0)
        expected = float(i.get("expected_budget") or 0.0)
        actual   = float(i.get("actual_cost") or 0.0)
        capped   = min(expected, budgeted)          # billing cap rule

        planned_qty = float(i.get("planned_quantity") or 0.0)
        actual_qty  = float(i.get("quantity") or 0.0)
        pct = round((actual_qty / planned_qty) * 100, 2) if planned_qty > 0 else 0.0

        rows.append({
            "period": period,
            "job_id": i.get("job_id") or "",
            "job_code": job_code.strip(),
            "job_description": job_description.strip(),
            "cost_code_id": i.get("cost_code_id") or "",
            "cost_code": cost_code,
            "description": i.get("cost_code_description") or "",
            "unit": i.get("unit") or "",
            "budgeted_quantity": planned_qty,
            "actual_quantity": actual_qty,
            "percent_complete": pct,
            "budgeted_all_cost": round(budgeted, 2),
            "expected_all_cost": round(expected, 2),
            "capped_expected_all_cost": round(capped, 2),
            "actual_all_cost": round(actual, 2),
            "variance": round(float(i.get("variance", expected - actual)), 2),
            "capped_variance": round(capped - actual, 2),
        })

    logger.info("Billing draft built: period=%s jobs=%d rows=%d",
                period, len({r["job_code"] for r in rows}), len(rows))
    return rows


def run_billing_draft_for_period(period: str) -> int:
    """Build *period* and store it (replacing any existing rows). Returns row count."""
    from api.database import SessionLocal
    from api.db_services import store_billing_draft

    rows = build_billing_draft_rows(period)
    db = SessionLocal()
    try:
        return store_billing_draft(db, period, rows)
    finally:
        db.close()


def previous_month(now=None) -> str:
    """The previous calendar month as 'YYYY-MM' (US Mountain time)."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    now = now or datetime.now(ZoneInfo("America/Denver"))
    year, month = (now.year - 1, 12) if now.month == 1 else (now.year, now.month - 1)
    return f"{year:04d}-{month:02d}"


def run_monthly_billing_draft() -> None:
    """Cron entry point: build + store the billing draft for the previous month."""
    period = previous_month()
    logger.info("Monthly billing draft job started for previous month=%s", period)
    try:
        stored = run_billing_draft_for_period(period)
        logger.info("Monthly billing draft complete: period=%s rows=%d", period, stored)
    except Exception:
        logger.exception("Monthly billing draft failed for period=%s", period)
