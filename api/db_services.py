"""
Database service layer - cache-aside pattern for quantity, budget, and projection.
"""
from __future__ import annotations
import json, logging, uuid
from datetime import datetime, timedelta, timezone
from typing import Any
from sqlalchemy.orm import Session
from sqlalchemy import and_, not_
from api.database import (
    TimelogVerificationCache, TimelogTimecardResult,
    QuantityTrackingCache, QuantityTrackingResult,
    QuantityTest,
    BudgetTrackingCache, BudgetTrackingResult,
    ProjectionTrackingCache,
    ProjectionTrackingResult,
    CronJobExecution,
    SyncState,
    BillingDraft,
    BillingDraftAIA,
    PostmortemJobSnapshot,
    PostmortemCostCodeResult,
)
logger = logging.getLogger(__name__)


def _calendar_date_list(start_date: str, end_date: str) -> list[str]:
    """Inclusive YYYY-MM-DD list from start_date through end_date."""
    dates: list[str] = []
    cur = datetime.fromisoformat(start_date).date()
    end = datetime.fromisoformat(end_date).date()
    while cur <= end:
        dates.append(cur.isoformat())
        cur += timedelta(days=1)
    return dates


def _is_daily_quantity_key(tracking_date: str) -> bool:
    """True for calendar-day keys only (excludes start__end period snapshots)."""
    return len(tracking_date) == 10 and "__" not in tracking_date

_BILLING_DRAFT_FIELDS = (
    "period", "job_id", "job_code", "job_description",
    "cost_code_id", "cost_code", "description", "unit",
    "budgeted_quantity", "actual_quantity", "percent_complete",
    "budgeted_all_cost", "expected_all_cost", "capped_expected_all_cost",
    "actual_all_cost", "variance", "capped_variance",
)


def store_billing_draft(db, period: str, rows: list[dict]) -> int:
    """Replace all billing-draft rows for *period* with *rows*. Returns count."""
    db.query(BillingDraft).filter(BillingDraft.period == period).delete()
    for r in rows:
        db.add(BillingDraft(id=str(uuid.uuid4()),
                            **{k: r.get(k) for k in _BILLING_DRAFT_FIELDS}))
    db.commit()
    logger.info("Stored billing draft: period=%s rows=%d", period, len(rows))
    return len(rows)


def get_billing_draft(db, period: str, job_code: str | None = None) -> list[dict]:
    """Flat billing-draft rows for *period* (optionally filtered to one job_code),
    ordered by job_code then cost_code."""
    q = db.query(BillingDraft).filter(BillingDraft.period == period)
    if job_code:
        q = q.filter(BillingDraft.job_code == job_code)
    rows = q.order_by(BillingDraft.job_code, BillingDraft.cost_code).all()
    return [{k: getattr(r, k) for k in _BILLING_DRAFT_FIELDS} for r in rows]


def billing_draft_job_codes(db, period: str) -> list[str]:
    """Distinct job codes that have billing-draft rows for *period*."""
    rows = db.query(BillingDraft.job_code).filter(
        BillingDraft.period == period).distinct().order_by(BillingDraft.job_code).all()
    return [r[0] for r in rows]


# ── BILLING DRAFT — AIA application -----------------------------------------

_AIA_SUMMARY_FIELDS = (
    "original_contract_sum", "net_change_by_co", "contract_sum_to_date",
    "total_completed_stored_to_date", "retainage_pct", "total_retainage",
    "total_earned_less_retainage", "less_previous_certificates",
    "current_payment_due", "gross_completed_this_period",
    "remaining_contract_balance", "items_billed", "items_held",
)


def store_billing_draft_aia(db, aia: dict) -> None:
    """Upsert one AIA application (replaces any existing row for period+job_code)."""
    period = aia["period"]
    job_code = aia["project"]["job_code"]
    db.query(BillingDraftAIA).filter(
        BillingDraftAIA.period == period, BillingDraftAIA.job_code == job_code).delete()
    s = aia["summary"]
    db.add(BillingDraftAIA(
        id=str(uuid.uuid4()), period=period,
        job_id=aia.get("job_id") or aia["project"].get("job_id"),
        job_code=job_code, job_description=aia["project"].get("job_description"),
        application_no=aia.get("application_no"), period_to=aia.get("period_to"),
        prepared_date=aia.get("prepared_date"),
        assumptions=json.dumps(aia.get("assumptions", [])),
        line_items=json.dumps(aia.get("line_items", [])),
        model_meta=json.dumps(aia.get("_meta", {})),
        **{k: s.get(k, 0) for k in _AIA_SUMMARY_FIELDS},
    ))
    db.commit()
    logger.info("Stored AIA draft: period=%s job=%s", period, job_code)


def _aia_row_to_dict(r) -> dict:
    return {
        "period": r.period,
        "project": {"job_code": r.job_code, "job_id": r.job_id,
                    "job_description": r.job_description},
        "application_no": r.application_no, "period_to": r.period_to,
        "prepared_date": r.prepared_date,
        "summary": {k: getattr(r, k) for k in _AIA_SUMMARY_FIELDS},
        "assumptions": json.loads(r.assumptions) if r.assumptions else [],
        "line_items": json.loads(r.line_items) if r.line_items else [],
        "_meta": json.loads(r.model_meta) if r.model_meta else {},
    }


def get_billing_draft_aia(db, period: str, job_code: str | None = None) -> list[dict]:
    q = db.query(BillingDraftAIA).filter(BillingDraftAIA.period == period)
    if job_code:
        q = q.filter(BillingDraftAIA.job_code == job_code)
    return [_aia_row_to_dict(r) for r in q.order_by(BillingDraftAIA.job_code).all()]


def get_prior_aia(db, job_code: str, before_period: str):
    """Most recent stored AIA for *job_code* in a period earlier than
    *before_period* — used for application carry-forward. Returns dict or None."""
    r = db.query(BillingDraftAIA).filter(
        BillingDraftAIA.job_code == job_code,
        BillingDraftAIA.period < before_period,
    ).order_by(BillingDraftAIA.period.desc()).first()
    return _aia_row_to_dict(r) if r else None


# ── TIMELOG ──────────────────────────────────────────────────────────────────

def store_timelog_verification(db, verification_date, business_unit_id, results):
    total = len(results)
    approved = sum(1 for r in results if r.get("status") == "APPROVED")
    flagged  = sum(1 for r in results if r.get("status") == "FLAGGED")
    rejected = sum(1 for r in results if r.get("status") == "REJECTED")
    entry = db.query(TimelogVerificationCache).filter(and_(
        TimelogVerificationCache.verification_date == verification_date,
        TimelogVerificationCache.business_unit_id  == business_unit_id,
    )).first()
    # Preserve portal sync status across re-syncs: the unified cron re-stores
    # these rows every 8h (delete + re-insert), which would otherwise wipe the
    # approve/reject status set by the 12:30 portal-sync job. Capture it first.
    prev_sync: dict[str, tuple] = {}
    if entry:
        entry.total_timecards = total; entry.approved_count = approved
        entry.flagged_count = flagged; entry.rejected_count = rejected
        entry.fetched_at = datetime.now(timezone.utc)
        entry.updated_at = datetime.now(timezone.utc)
        cache_id = entry.id
        old_rows = db.query(TimelogTimecardResult).filter(and_(
            TimelogTimecardResult.verification_date == verification_date,
            TimelogTimecardResult.business_unit_id  == business_unit_id,
        )).all()
        prev_sync = {
            row.timecard_id: (getattr(row, "portal_sync_status", "pending") or "pending",
                              getattr(row, "portal_synced_at", None))
            for row in old_rows
        }
        for row in old_rows:
            db.delete(row)
    else:
        cache_id = str(uuid.uuid4())
        db.add(TimelogVerificationCache(id=cache_id,
            verification_date=verification_date, business_unit_id=business_unit_id,
            total_timecards=total, approved_count=approved,
            flagged_count=flagged, rejected_count=rejected))
    for r in results:
        sync_status, synced_at = prev_sync.get(r.get("id", ""), ("pending", None))
        db.add(TimelogTimecardResult(id=str(uuid.uuid4()),
            verification_date=verification_date, business_unit_id=business_unit_id,
            timecard_id=r.get("id",""), job_id=r.get("job_id"),
            job_code=r.get("job",""), job_description=r.get("job_description","") or "",
            foreman_id=r.get("foreman_id"),
            foreman=r.get("foreman",""), status=r.get("status","APPROVED"),
            reasons=json.dumps(r.get("reasons",[])),
            flags=json.dumps(r.get("flags",[])), why=r.get("why",""),
            portal_sync_status=sync_status, portal_synced_at=synced_at))
    db.commit()
    logger.info("Stored timelog: date=%s records=%d", verification_date, total)
    return cache_id, total


def get_timelog_verification(db, verification_date, business_unit_id=None):
    entry = db.query(TimelogVerificationCache).filter(and_(
        TimelogVerificationCache.verification_date == verification_date,
        TimelogVerificationCache.business_unit_id  == business_unit_id,
    )).first()
    if not entry:
        return None, []
    summary = {"date": verification_date, "total": entry.total_timecards,
        "approved": entry.approved_count, "flagged": entry.flagged_count,
        "rejected": entry.rejected_count, "fetched_at": entry.fetched_at.isoformat()}
    rows = db.query(TimelogTimecardResult).filter(and_(
        TimelogTimecardResult.verification_date == verification_date,
        TimelogTimecardResult.business_unit_id  == business_unit_id,
    )).all()
    results = [{"id": tc.timecard_id, "date": verification_date,
        "job": tc.job_code, "job_id": tc.job_id,
        "job_description": getattr(tc, "job_description", "") or "",
        "foreman": tc.foreman, "foreman_id": tc.foreman_id, "status": tc.status,
        "reasons": json.loads(tc.reasons) if tc.reasons else [],
        "flags": json.loads(tc.flags) if tc.flags else [],
        "portal_sync_status": getattr(tc, "portal_sync_status", "pending") or "pending",
        "portal_synced_at": tc.portal_synced_at.isoformat() if getattr(tc, "portal_synced_at", None) else None,
        "why": tc.why} for tc in rows]
    return summary, results


def update_portal_sync_status(db, verification_date, business_unit_id, actions) -> int:
    """Write back the browser-automation outcome onto each timecard row.

    *actions* is the list returned by payroll_automation.run_portal_actions.
    Dry-run actions ('would_*') are ignored (nothing actually happened).
    Returns the number of rows updated.
    """
    status_map = {
        "approve": "approved", "reject": "rejected",
        "error": "failed", "not_found": "not_found", "skipped": "skipped",
    }
    now = datetime.now(timezone.utc)
    updated = 0
    for a in actions or []:
        action = (a.get("action") or "")
        if action.startswith("would_"):
            continue  # dry-run — no real portal change
        new_status = status_map.get(action)
        if not new_status:
            continue
        updated += db.query(TimelogTimecardResult).filter(and_(
            TimelogTimecardResult.verification_date == verification_date,
            TimelogTimecardResult.business_unit_id  == business_unit_id,
            TimelogTimecardResult.timecard_id       == a.get("timecard_id"),
        )).update(
            {"portal_sync_status": new_status, "portal_synced_at": now},
            synchronize_session=False,
        )
    db.commit()
    logger.info("Portal sync status updated for %d row(s) on %s", updated, verification_date)
    return updated


# ── QUANTITY ─────────────────────────────────────────────────────────────────

def store_quantity_tracking(db, tracking_date, results):
    total    = len(results)
    on_track = sum(1 for r in results if r.get("status") == "ON_TRACK")
    near     = sum(1 for r in results if r.get("status") == "NEAR_COMPLETION")
    over     = sum(1 for r in results if r.get("status") == "OVER_RISK")
    entry = db.query(QuantityTrackingCache).filter(
        QuantityTrackingCache.tracking_date == tracking_date).first()
    if entry:
        entry.total_cost_codes = total; entry.on_track_count = on_track
        entry.near_completion_count = near; entry.over_risk_count = over
        entry.fetched_at = datetime.now(timezone.utc)
        entry.updated_at = datetime.now(timezone.utc)
        cache_id = entry.id
        db.query(QuantityTrackingResult).filter(
            QuantityTrackingResult.tracking_date == tracking_date).delete()
    else:
        cache_id = str(uuid.uuid4())
        db.add(QuantityTrackingCache(id=cache_id, tracking_date=tracking_date,
            total_cost_codes=total, on_track_count=on_track,
            near_completion_count=near, over_risk_count=over))
    for r in results:
        db.add(QuantityTrackingResult(id=str(uuid.uuid4()),
            tracking_date=tracking_date,
            cost_code_id=r.get("cost_code_id",""), cost_code=r.get("cost_code",""),
            description=r.get("description",""), job_id=r.get("job_id",""),
            job_code=r.get("job_code",""), unit=r.get("unit",""),
            cost_type=r.get("cost_type",""),
            planned_quantity=float(r.get("planned_quantity",0)),
            installed_quantity=float(r.get("installed_quantity",0)),
            remaining_quantity=float(r.get("remaining_quantity",0)),
            percent_complete=float(r.get("percent_complete",0)),
            status=r.get("status","ON_TRACK"), alert=r.get("alert",False)))
    db.commit()
    logger.info("Stored quantity: date=%s records=%d", tracking_date, total)
    return cache_id, total


def get_quantity_tracking(db, tracking_date):
    entry = db.query(QuantityTrackingCache).filter(
        QuantityTrackingCache.tracking_date == tracking_date).first()
    if not entry:
        return None, []
    summary = {"date": tracking_date, "total": entry.total_cost_codes,
        "on_track": entry.on_track_count, "near_completion": entry.near_completion_count,
        "over_risk": entry.over_risk_count, "fetched_at": entry.fetched_at.isoformat()}
    rows = db.query(QuantityTrackingResult).filter(
        QuantityTrackingResult.tracking_date == tracking_date).all()
    results = [{"cost_code_id": r.cost_code_id, "cost_code": r.cost_code,
        "description": r.description, "job_id": r.job_id, "job_code": r.job_code,
        "unit": r.unit, "cost_type": r.cost_type,
        "planned_quantity": r.planned_quantity, "installed_quantity": r.installed_quantity,
        "remaining_quantity": r.remaining_quantity, "percent_complete": r.percent_complete,
        "status": r.status, "alert": r.alert} for r in rows]
    return summary, results


# ── QUANTITY TEST (isolated — daily rows only; sum on read) ───────────────────

def quantity_test_has_data(db, tracking_date: str) -> bool:
    """True if quantity_test already has at least one row for this calendar day."""
    row = (
        db.query(QuantityTest.id)
        .filter(QuantityTest.tracking_date == tracking_date)
        .first()
    )
    return row is not None


def store_quantity_test(db, tracking_date: str, results: list[dict]) -> int:
    """
    Overwrite all quantity_test rows for one calendar day (YYYY-MM-DD).
    Does not touch quantity_tracking_* production tables.
    """
    db.query(QuantityTest).filter(
        QuantityTest.tracking_date == tracking_date
    ).delete()
    for r in results:
        db.add(QuantityTest(
            id=str(uuid.uuid4()),
            tracking_date=tracking_date,
            cost_code_id=r.get("cost_code_id", ""),
            cost_code=r.get("cost_code", ""),
            description=r.get("description", ""),
            job_id=r.get("job_id", ""),
            job_code=r.get("job_code", ""),
            unit=r.get("unit", ""),
            cost_type=r.get("cost_type", ""),
            planned_quantity=float(r.get("planned_quantity", 0)),
            installed_quantity=float(r.get("installed_quantity", 0)),
            remaining_quantity=float(r.get("remaining_quantity", 0)),
            percent_complete=float(r.get("percent_complete", 0)),
            status=r.get("status", "ON_TRACK"),
            alert=bool(r.get("alert", False)),
        ))
    db.commit()
    logger.info(
        "Stored quantity_test: date=%s records=%d", tracking_date, len(results)
    )
    return len(results)


def get_quantity_test_days_present(db, start_date: str, end_date: str) -> list[str]:
    """Return distinct tracking_date values in [start_date, end_date] that have rows."""
    rows = (
        db.query(QuantityTest.tracking_date)
        .filter(
            QuantityTest.tracking_date >= start_date,
            QuantityTest.tracking_date <= end_date,
        )
        .distinct()
        .order_by(QuantityTest.tracking_date.asc())
        .all()
    )
    return [r[0] for r in rows]


def sum_quantity_test_for_period(
    db, start_date: str, end_date: str
) -> list[dict]:
    """
    Load daily quantity_test rows in [start_date, end_date] and aggregate by
    (job_id, cost_code_id): SUM(installed_quantity), keep planned from rows.
    Recalculate percent_complete / status / alert for the period total.
    """
    rows = (
        db.query(QuantityTest)
        .filter(
            QuantityTest.tracking_date >= start_date,
            QuantityTest.tracking_date <= end_date,
        )
        .all()
    )
    if not rows:
        return []

    agg: dict[tuple, dict] = {}
    for r in rows:
        key = (r.job_id, r.cost_code_id)
        if key in agg:
            agg[key]["installed_quantity"] += float(r.installed_quantity or 0)
            # planned is static metadata — keep latest non-zero if present
            if float(r.planned_quantity or 0) > 0:
                agg[key]["planned_quantity"] = float(r.planned_quantity)
        else:
            agg[key] = {
                "cost_code_id": r.cost_code_id,
                "cost_code": r.cost_code,
                "description": r.description,
                "job_id": r.job_id,
                "job_code": r.job_code,
                "unit": r.unit,
                "cost_type": r.cost_type,
                "planned_quantity": float(r.planned_quantity or 0),
                "installed_quantity": float(r.installed_quantity or 0),
            }

    results: list[dict] = []
    for item in agg.values():
        planned = float(item["planned_quantity"])
        installed = round(float(item["installed_quantity"]), 4)
        percent = round((installed / planned) * 100, 2) if planned > 0 else 0.0
        if percent >= 100.0:
            st = "OVER_RISK"
        elif percent >= 75.0:
            st = "NEAR_COMPLETION"
        else:
            st = "ON_TRACK"
        results.append({
            "cost_code_id": item["cost_code_id"],
            "cost_code": item["cost_code"],
            "description": item["description"],
            "job_id": item["job_id"],
            "job_code": item["job_code"],
            "unit": item["unit"],
            "cost_type": item["cost_type"],
            "planned_quantity": planned,
            "installed_quantity": installed,
            "remaining_quantity": round(planned - installed, 4),
            "percent_complete": percent,
            "status": st,
            "alert": percent >= 75.0,
        })
    return results


# ── BUDGET ───────────────────────────────────────────────────────────────────

def store_budget_tracking(db, tracking_date, results):
    total    = len(results)
    on_track = sum(1 for r in results if r.get("status") == "ON_TRACK")
    over     = sum(1 for r in results if r.get("status") == "OVER_RISK")
    entry = db.query(BudgetTrackingCache).filter(
        BudgetTrackingCache.tracking_date == tracking_date).first()
    if entry:
        entry.total_cost_codes = total; entry.on_track_count = on_track
        entry.over_risk_count = over
        entry.fetched_at = datetime.now(timezone.utc)
        entry.updated_at = datetime.now(timezone.utc)
        cache_id = entry.id
        db.query(BudgetTrackingResult).filter(
            BudgetTrackingResult.tracking_date == tracking_date).delete()
    else:
        cache_id = str(uuid.uuid4())
        db.add(BudgetTrackingCache(id=cache_id, tracking_date=tracking_date,
            total_cost_codes=total, on_track_count=on_track, over_risk_count=over))
    for r in results:
        raw_util = r.get("utilization_percentage")
        util_val = int(raw_util) if raw_util is not None else None
        db.add(BudgetTrackingResult(
            id=str(uuid.uuid4()),
            tracking_date=tracking_date,
            cost_code_id=r.get("cost_code_id", ""),
            cost_code=r.get("cost_code", ""),
            cost_code_description=r.get("cost_code_description", ""),
            job_id=r.get("job_id", ""),
            job_name=r.get("job_name", ""),
            business_unit=r.get("business_unit"),
            # Store raw HCSS total budget — expected_budget is computed on the fly
            budgeted_all_cost=float(r.get("budgeted_all_cost", 0)),
            actual_cost=float(r.get("actual_cost", 0)),
            # utilization_percentage / variance / status are computed at response time
            # and not stored — these DB columns are not relied on by any route
            utilization_percentage=util_val,
            variance=float(r.get("variance", 0)),
            status=r.get("status", "ON_TRACK"),
            labor_cost=float(r.get("labor_cost", 0)),
            equipment_cost=float(r.get("equipment_cost", 0)),
            material_cost=float(r.get("material_cost", 0)),
            subcontract_cost=float(r.get("subcontract_cost", 0)),
            trucking_cost=float(r.get("trucking_cost", 0)),
            labor_hours=float(r.get("labor_hours", 0)),
            quantity=float(r.get("quantity", 0)),
            labor_budget=float(r.get("labor_budget", 0)),
            equipment_budget=float(r.get("equipment_budget", 0)),
            material_budget=float(r.get("material_budget", 0)),
            subcontract_budget=float(r.get("subcontract_budget", 0)),
            planned_quantity=float(r.get("planned_quantity", 0)),
            unit=r.get("unit", "") or "",
        ))
    db.commit()
    logger.info("Stored budget: date=%s records=%d", tracking_date, total)
    return cache_id, total


def get_budget_tracking(db, tracking_date):
    entry = db.query(BudgetTrackingCache).filter(
        BudgetTrackingCache.tracking_date == tracking_date).first()
    if not entry:
        return None, []
    summary = {"date": tracking_date, "total": entry.total_cost_codes,
        "on_track": entry.on_track_count, "over_risk": entry.over_risk_count,
        "fetched_at": entry.fetched_at.isoformat()}
    rows = db.query(BudgetTrackingResult).filter(
        BudgetTrackingResult.tracking_date == tracking_date).all()
    results = [{
        "cost_code_id":          r.cost_code_id,
        "cost_code":             r.cost_code,
        "cost_code_description": r.cost_code_description,
        "job_id":                r.job_id,
        "job_name":              r.job_name,
        "business_unit":         r.business_unit,
        # budgeted_all_cost is the raw HCSS total — expected_budget computed on the fly
        "budgeted_all_cost":     r.budgeted_all_cost,
        "actual_cost":           r.actual_cost,
        # utilization/variance/status are NOT read from DB — computed at response time
        "labor_cost":       getattr(r, "labor_cost",       0.0) or 0.0,
        "equipment_cost":   getattr(r, "equipment_cost",   0.0) or 0.0,
        "material_cost":    getattr(r, "material_cost",    0.0) or 0.0,
        "subcontract_cost": getattr(r, "subcontract_cost", 0.0) or 0.0,
        "trucking_cost":    getattr(r, "trucking_cost",    0.0) or 0.0,
        "labor_hours":      getattr(r, "labor_hours",      0.0) or 0.0,
        "quantity":         getattr(r, "quantity",         0.0) or 0.0,
        "labor_budget":     getattr(r, "labor_budget",     0.0) or 0.0,
        "equipment_budget": getattr(r, "equipment_budget", 0.0) or 0.0,
        "material_budget":  getattr(r, "material_budget",  0.0) or 0.0,
        "subcontract_budget": getattr(r, "subcontract_budget", 0.0) or 0.0,
        "planned_quantity": getattr(r, "planned_quantity", 0.0) or 0.0,
        "unit":             getattr(r, "unit",             "") or "",
        "foremen": [],
    } for r in rows]
    return summary, results


# ── CACHE-ASIDE HELPERS ──────────────────────────────────────────────────────

def _quantity_result_to_dict(r) -> dict:
    return {
        "cost_code_id": r.cost_code_id,
        "cost_code": r.cost_code,
        "description": r.description,
        "job_id": r.job_id,
        "job_code": r.job_code,
        "unit": r.unit,
        "cost_type": r.cost_type,
        "planned_quantity": r.planned_quantity,
        "installed_quantity": r.installed_quantity,
        "remaining_quantity": r.remaining_quantity,
        "percent_complete": r.percent_complete,
        "status": r.status,
        "alert": r.alert,
    }


def sum_quantity_tracking_for_period(
    db: Session,
    start_date: str,
    end_date: str,
) -> list[dict]:
    """
    Aggregate production daily quantity rows in [start_date, end_date].

    Only includes calendar-day keys (YYYY-MM-DD). Period snapshot keys
    (start__end) are never included.
    """
    days = _calendar_date_list(start_date, end_date)
    if not days:
        return []

    rows = (
        db.query(QuantityTrackingResult)
        .filter(QuantityTrackingResult.tracking_date.in_(days))
        .all()
    )
    if not rows:
        return []

    agg: dict[tuple, dict] = {}
    for r in rows:
        if not _is_daily_quantity_key(r.tracking_date):
            continue
        key = (r.job_id, r.cost_code_id)
        if key in agg:
            agg[key]["installed_quantity"] += float(r.installed_quantity or 0)
            if float(r.planned_quantity or 0) > 0:
                agg[key]["planned_quantity"] = float(r.planned_quantity)
        else:
            agg[key] = {
                "cost_code_id": r.cost_code_id,
                "cost_code": r.cost_code,
                "description": r.description,
                "job_id": r.job_id,
                "job_code": r.job_code,
                "unit": r.unit,
                "cost_type": r.cost_type,
                "planned_quantity": float(r.planned_quantity or 0),
                "installed_quantity": float(r.installed_quantity or 0),
            }

    results: list[dict] = []
    for item in agg.values():
        planned = float(item["planned_quantity"])
        installed = round(float(item["installed_quantity"]), 4)
        percent = round((installed / planned) * 100, 2) if planned > 0 else 0.0
        if percent >= 100.0:
            st = "OVER_RISK"
        elif percent >= 75.0:
            st = "NEAR_COMPLETION"
        else:
            st = "ON_TRACK"
        results.append({
            "cost_code_id": item["cost_code_id"],
            "cost_code": item["cost_code"],
            "description": item["description"],
            "job_id": item["job_id"],
            "job_code": item["job_code"],
            "unit": item["unit"],
            "cost_type": item["cost_type"],
            "planned_quantity": planned,
            "installed_quantity": installed,
            "remaining_quantity": round(planned - installed, 4),
            "percent_complete": percent,
            "status": st,
            "alert": percent >= 75.0,
        })
    return results


def quantity_day_present(db: Session, tracking_date: str) -> bool:
    """True if a daily cache header exists (even when detail rows are empty)."""
    if not _is_daily_quantity_key(tracking_date):
        return False
    summary, _ = get_quantity_tracking(db, tracking_date)
    return summary is not None


def get_or_fetch_quantity_for_date(db: Session, target_date: str) -> list[dict]:
    summary, cached = get_quantity_tracking(db, target_date)
    if summary is not None:
        logger.info("Quantity DB HIT  date=%s records=%d", target_date, len(cached))
        return cached

    logger.info("Quantity DB MISS date=%s  -> fetching HCSS", target_date)
    try:
        from quantity_tracking.tracker import track_quantities
        hcss = track_quantities(target_date=target_date)
        dicts = [_quantity_result_to_dict(r) for r in hcss] if hcss else []
        store_quantity_tracking(db, target_date, dicts)
        logger.info("Quantity stored from HCSS date=%s records=%d", target_date, len(dicts))
        return dicts
    except Exception as exc:
        logger.error("Quantity HCSS fetch failed date=%s: %s", target_date, exc)
        return []


def get_or_fetch_quantity_for_period(db: Session, start_date: str, end_date: str) -> list[dict]:
    """
    Period quantity = SUM of daily production rows.

    Ensures each calendar day in the range has a cache header (fetching
    missing days from HCSS). Never reads or writes start__end period keys.
    """
    days = _calendar_date_list(start_date, end_date)
    missing = [d for d in days if not quantity_day_present(db, d)]

    if missing:
        logger.info(
            "Quantity period %s→%s: %d/%d day(s) missing — fetching from HCSS",
            start_date, end_date, len(missing), len(days),
        )
        from quantity_tracking.tracker import track_quantities
        for day in missing:
            try:
                hcss = track_quantities(start_date=day, end_date=day)
                dicts = [_quantity_result_to_dict(r) for r in hcss] if hcss else []
                store_quantity_tracking(db, day, dicts)
                logger.info(
                    "Quantity day filled: %s records=%d", day, len(dicts),
                )
            except Exception as exc:
                logger.error(
                    "Quantity HCSS fetch failed day=%s: %s — period sum may undercount",
                    day, exc,
                )
    else:
        logger.info(
            "Quantity period %s→%s: all %d day(s) present — summing daily rows",
            start_date, end_date, len(days),
        )

    return sum_quantity_tracking_for_period(db, start_date, end_date)


def get_or_fetch_budget_for_date(db: Session, target_date: str) -> list[dict]:
    _, cached = get_budget_tracking(db, target_date)
    if cached:
        logger.info("Budget DB HIT  date=%s records=%d", target_date, len(cached))
        return cached

    logger.info("Budget DB MISS date=%s  -> fetching HCSS", target_date)
    try:
        from budget_tracking.hcss_client import BudgetHCSSClient
        from budget_tracking.analyzer import calculate_budget_status

        raw = BudgetHCSSClient().fetch_budget_summary(date=target_date)
        if not raw:
            logger.info("HCSS returned 0 budget records for date=%s", target_date)
            return []

        budget_results = calculate_budget_status(raw)
        raw_by_key: dict[tuple, dict] = {}
        for item in raw:
            jid  = (item.get("job")      or {}).get("jobId",      "")
            ccid = (item.get("costCode") or {}).get("costCodeId", "")
            if jid and ccid:
                raw_by_key[(jid, ccid)] = item

        dicts: list[dict] = []
        for r in budget_results:
            extra = raw_by_key.get((r.job_id, r.cost_code_id), {})
            dicts.append({
                "cost_code_id":          r.cost_code_id,
                "cost_code":             r.cost_code,
                "cost_code_description": r.cost_code_description,
                "job_id":                r.job_id,
                "job_name":              r.job_name,
                "business_unit":         r.business_unit,
                "budgeted_all_cost":     r.budgeted_all_cost,
                "actual_cost":           r.actual_cost,
                "labor_cost":       extra.get("laborCost",       0.0),
                "equipment_cost":   extra.get("equipmentCost",   0.0),
                "material_cost":    extra.get("materialCost",    0.0),
                "subcontract_cost": extra.get("subcontractCost", 0.0),
                "trucking_cost":    extra.get("truckingCost",    0.0),
                "labor_hours":      extra.get("laborHours",      0.0),
                "quantity":         extra.get("quantity",        0.0),
                "labor_budget":       extra.get("laborBudget",       0.0),
                "equipment_budget":   extra.get("equipmentBudget",   0.0),
                "material_budget":    extra.get("materialBudget",    0.0),
                "subcontract_budget": extra.get("subcontractBudget", 0.0),
                "planned_quantity":   float(extra.get("plannedQuantity", 0.0)),
                "unit":               extra.get("unit", "") or "",
                "foremen":            extra.get("foremen",           []),
            })

        store_budget_tracking(db, target_date, dicts)
        logger.info("Budget stored from HCSS date=%s records=%d", target_date, len(dicts))
        return dicts
    except Exception as exc:
        logger.error("Budget HCSS fetch failed date=%s: %s", target_date, exc)
        return []


# ── SYNC STATE ───────────────────────────────────────────────────────────────

BUDGET_LAST_SYNC_KEY = "budget_last_sync_at"
QUANTITY_LAST_SYNC_KEY = "quantity_last_sync_at"
QUANTITY_PERIOD_KEYS_CLEANED = "quantity_period_keys_cleaned"


def get_sync_state(db: Session, key: str) -> str | None:
    """Return the stored sync cursor value for key, or None if unset."""
    row = db.query(SyncState).filter(SyncState.key == key).first()
    return row.value if row else None


def set_sync_state(db: Session, key: str, value: str) -> None:
    """Upsert a sync cursor value."""
    row = db.query(SyncState).filter(SyncState.key == key).first()
    now = datetime.now(timezone.utc)
    if row:
        row.value = value
        row.updated_at = now
    else:
        db.add(SyncState(key=key, value=value, updated_at=now))
    db.commit()
    logger.info("Sync state updated: %s=%s", key, value)


def get_budget_last_sync_at(db: Session) -> datetime | None:
    """Parse budget_last_sync_at cursor as timezone-aware UTC datetime."""
    raw = get_sync_state(db, BUDGET_LAST_SYNC_KEY)
    if not raw:
        return None
    try:
        s = raw.strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        logger.warning("Invalid budget_last_sync_at value %r — treating as unset", raw)
        return None


def set_budget_last_sync_at(db: Session, when: datetime) -> None:
    """Persist budget sync cursor (always store UTC ISO-8601)."""
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    else:
        when = when.astimezone(timezone.utc)
    set_sync_state(db, BUDGET_LAST_SYNC_KEY, when.isoformat().replace("+00:00", "Z"))


def get_quantity_last_sync_at(db: Session) -> datetime | None:
    """Parse quantity_last_sync_at cursor as timezone-aware UTC datetime."""
    raw = get_sync_state(db, QUANTITY_LAST_SYNC_KEY)
    if not raw:
        return None
    try:
        s = raw.strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        logger.warning("Invalid quantity_last_sync_at value %r — treating as unset", raw)
        return None


def set_quantity_last_sync_at(db: Session, when: datetime) -> None:
    """Persist quantity sync cursor (always store UTC ISO-8601)."""
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    else:
        when = when.astimezone(timezone.utc)
    set_sync_state(db, QUANTITY_LAST_SYNC_KEY, when.isoformat().replace("+00:00", "Z"))


def cleanup_quantity_period_cache_keys(db: Session) -> int:
    """
    One-time delete of legacy start__end period snapshot keys.

    Daily-sum readers must never mix these with calendar-day rows.
    Idempotent via sync_state flag quantity_period_keys_cleaned.

    IMPORTANT: do not use SQL LIKE/contains with bare '_' — in SQL LIKE,
    '_' is a single-character wildcard and would match every YYYY-MM-DD key.
    Period keys are longer than 10 characters.
    """
    from sqlalchemy import func

    if get_sync_state(db, QUANTITY_PERIOD_KEYS_CLEANED) == "1":
        return 0

    result_count = (
        db.query(QuantityTrackingResult)
        .filter(func.length(QuantityTrackingResult.tracking_date) > 10)
        .delete(synchronize_session=False)
    )
    cache_count = (
        db.query(QuantityTrackingCache)
        .filter(func.length(QuantityTrackingCache.tracking_date) > 10)
        .delete(synchronize_session=False)
    )
    db.commit()
    set_sync_state(db, QUANTITY_PERIOD_KEYS_CLEANED, "1")
    logger.info(
        "Cleaned quantity period cache keys: results=%d cache=%d",
        result_count, cache_count,
    )
    return int(result_count) + int(cache_count)

# ── CRON JOB TRACKING ────────────────────────────────────────────────────────

def create_cron_job_execution(db, job_type, execution_date, max_retries=3):
    existing = db.query(CronJobExecution).filter(and_(
        CronJobExecution.job_type == job_type,
        CronJobExecution.execution_date == execution_date,
        CronJobExecution.status.in_(["pending", "failed", "retrying", "running"]),
    )).first()

    if existing:
        existing.status = "pending"
        existing.error_message = None
        existing.records_processed = None
        existing.started_at = None
        existing.completed_at = None
        existing.next_retry_at = None
        existing.updated_at = datetime.now(timezone.utc)
        db.commit()
        logger.debug("Reusing existing cron job record %s for %s/%s", existing.id[:8], job_type, execution_date)
        return existing.id

    job_id = str(uuid.uuid4())
    db.add(CronJobExecution(id=job_id, job_type=job_type,
        execution_date=execution_date, status="pending",
        attempt_count=1, max_retries=max_retries))
    db.commit()
    return job_id


def update_cron_job_status(db, job_id, status, records_processed=None, error_message=None):
    job = db.query(CronJobExecution).filter(CronJobExecution.id == job_id).first()
    if not job:
        logger.warning("Cron job not found: %s", job_id)
        return
    job.status = status
    if records_processed is not None:
        job.records_processed = records_processed
    if error_message:
        job.error_message = error_message
    if status == "running" and not job.started_at:
        job.started_at = datetime.now(timezone.utc)
    elif status in ("success", "failed"):
        job.completed_at = datetime.now(timezone.utc)
    job.updated_at = datetime.now(timezone.utc)
    db.commit()


def get_pending_retry_jobs(db):
    now = datetime.now(timezone.utc)
    return db.query(CronJobExecution).filter(and_(
        CronJobExecution.status == "retrying",
        CronJobExecution.next_retry_at <= now,
        CronJobExecution.attempt_count < CronJobExecution.max_retries,
    )).all()


def schedule_retry(db: Session, job_id: str, retry_delay_minutes: int = 30) -> None:
    job_execution = db.query(CronJobExecution).filter(CronJobExecution.id == job_id).first()
    if not job_execution:
        return

    from datetime import timedelta

    delay_multiplier = 2 ** (job_execution.attempt_count - 1)
    actual_delay = retry_delay_minutes * delay_multiplier

    job_execution.status = "retrying"
    job_execution.attempt_count += 1
    job_execution.next_retry_at = datetime.now(timezone.utc) + timedelta(minutes=actual_delay)
    job_execution.updated_at = datetime.now(timezone.utc)
    db.commit()

    logger.info("Scheduled retry for job %s: attempt %d/%d in %d minutes",
        job_id, job_execution.attempt_count, job_execution.max_retries, actual_delay)


def check_if_date_processed(db, job_type, execution_date):
    return db.query(CronJobExecution).filter(and_(
        CronJobExecution.job_type == job_type,
        CronJobExecution.execution_date == execution_date,
        CronJobExecution.status == "success",
    )).first() is not None


# ── PROJECTION TRACKING ──────────────────────────────────────────────────────

def _projection_cache_key(start_date: str, end_date: str) -> str:
    return start_date if start_date == end_date else f"{start_date}__{end_date}"


def _safe_float(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def store_projection_tracking(db, tracking_month: str, results: list) -> tuple:
    """
    Upsert projection data into the database.

    tracking_month is kept for compatibility, but it can now be either:
        - YYYY-MM for monthly cache
        - YYYY-MM-DD__YYYY-MM-DD for custom date-range cache
    """
    total = len(results)
    on_track = sum(1 for r in results if r.get("status") == "ON_TRACK")
    at_risk = sum(1 for r in results if r.get("status") == "AT_RISK")
    over_budget = sum(1 for r in results if r.get("status") == "OVER_BUDGET")
    alerts = sum(1 for r in results if r.get("alert", False))

    entry = db.query(ProjectionTrackingCache).filter(
        ProjectionTrackingCache.tracking_month == tracking_month
    ).first()

    if entry:
        entry.total_jobs = total
        entry.on_track_count = on_track
        entry.at_risk_count = at_risk
        entry.over_budget_count = over_budget
        entry.alerts_count = alerts
        entry.fetched_at = datetime.now(timezone.utc)
        entry.updated_at = datetime.now(timezone.utc)
        cache_id = entry.id
        db.query(ProjectionTrackingResult).filter(
            ProjectionTrackingResult.tracking_month == tracking_month
        ).delete()
    else:
        cache_id = str(uuid.uuid4())
        db.add(ProjectionTrackingCache(
            id=cache_id,
            tracking_month=tracking_month,
            total_jobs=total,
            on_track_count=on_track,
            at_risk_count=at_risk,
            over_budget_count=over_budget,
            alerts_count=alerts,
        ))

    for r in results:
        db.add(ProjectionTrackingResult(
            id=str(uuid.uuid4()),
            tracking_month=tracking_month,
            period_start=r.get("period_start", ""),
            period_end=r.get("period_end", ""),
            job_id=r.get("job_id", ""),
            job_code=r.get("job_code", ""),
            job_name=r.get("job_name", ""),
            business_unit=r.get("business_unit"),
            cost_code_id=r.get("cost_code_id", ""),
            cost_code=r.get("cost_code", ""),
            cost_code_description=r.get("cost_code_description", ""),
            unit=r.get("unit", ""),
            budgeted_quantity=_safe_float(r.get("budgeted_quantity")),
            quantity=_safe_float(r.get("quantity")),
            completion_pct=_safe_float(r.get("completion_pct")),
            budgeted_cost=_safe_float(r.get("budgeted_cost")),
            expected=_safe_float(r.get("expected")),
            actual=_safe_float(r.get("actual")),
            variance=_safe_float(r.get("variance")),
            projected_final=_safe_float(r.get("projected_final")),
            projected_over_under=_safe_float(r.get("projected_over_under")),
            performance_factor=_safe_float(r.get("performance_factor")),
            actual_labor_cost=_safe_float(r.get("actual_labor_cost")),
            actual_equipment_cost=_safe_float(r.get("actual_equipment_cost")),
            actual_material_cost=_safe_float(r.get("actual_material_cost")),
            actual_subcontract_cost=_safe_float(r.get("actual_subcontract_cost")),
            actual_trucking_cost=_safe_float(r.get("actual_trucking_cost")),
            quantity_from_job_costs=_safe_float(r.get("quantity_from_job_costs")),
            labor_hours=_safe_float(r.get("labor_hours")),
            equipment_hours=_safe_float(r.get("equipment_hours")),
            original_contract_value=0.0,
            approved_change_orders=0.0,
            revised_contract_value=0.0,
            original_budget=_safe_float(r.get("budgeted_cost")),
            actual_cost_to_date=_safe_float(r.get("actual")),
            hcss_forecast_cost=_safe_float(r.get("expected")),
            projected_final_cost=_safe_float(r.get("projected_final")),
            cost_variance=_safe_float(r.get("projected_over_under")),
            forecast_source="production_analysis",
            percent_complete=_safe_float(r.get("completion_pct")),
            estimated_completion_month=r.get("period_end", "")[:7],
            billed_to_date=0.0,
            projected_final_billing=0.0,
            billing_variance=0.0,
            status=r.get("status", "ON_TRACK"),
            alert=r.get("alert", False),
            discrepancy_flag=r.get("discrepancy_flag", False),
        ))

    db.commit()
    logger.info("Stored projection: key=%s records=%d", tracking_month, total)
    return cache_id, total


def get_projection_tracking(db, tracking_month: str) -> tuple:
    entry = db.query(ProjectionTrackingCache).filter(
        ProjectionTrackingCache.tracking_month == tracking_month
    ).first()

    if not entry:
        return None, []

    summary = {
        "key": tracking_month,
        "month": tracking_month,
        "total": entry.total_jobs,
        "on_track": entry.on_track_count,
        "at_risk": entry.at_risk_count,
        "over_budget": entry.over_budget_count,
        "alerts": entry.alerts_count,
        "fetched_at": entry.fetched_at.isoformat(),
    }

    rows = db.query(ProjectionTrackingResult).filter(
        ProjectionTrackingResult.tracking_month == tracking_month
    ).all()

    results = [
        {
            "period_start": getattr(r, "period_start", "") or "",
            "period_end": getattr(r, "period_end", "") or "",
            "job_id": r.job_id,
            "job_code": r.job_code,
            "job_name": r.job_name,
            "business_unit": r.business_unit,
            "cost_code_id": getattr(r, "cost_code_id", "") or "",
            "cost_code": getattr(r, "cost_code", "") or "",
            "cost_code_description": getattr(r, "cost_code_description", "") or "",
            "unit": getattr(r, "unit", "") or "",
            "budgeted_quantity": getattr(r, "budgeted_quantity", 0.0) or 0.0,
            "quantity": getattr(r, "quantity", 0.0) or 0.0,
            "completion_pct": getattr(r, "completion_pct", 0.0) or 0.0,
            "budgeted_cost": getattr(r, "budgeted_cost", 0.0) or 0.0,
            "expected": getattr(r, "expected", 0.0) or 0.0,
            "actual": getattr(r, "actual", 0.0) or 0.0,
            "variance": getattr(r, "variance", 0.0) or 0.0,
            "projected_final": getattr(r, "projected_final", 0.0) or 0.0,
            "projected_over_under": getattr(r, "projected_over_under", 0.0) or 0.0,
            "performance_factor": getattr(r, "performance_factor", 0.0) or 0.0,
            "actual_labor_cost": getattr(r, "actual_labor_cost", 0.0) or 0.0,
            "actual_equipment_cost": getattr(r, "actual_equipment_cost", 0.0) or 0.0,
            "actual_material_cost": getattr(r, "actual_material_cost", 0.0) or 0.0,
            "actual_subcontract_cost": getattr(r, "actual_subcontract_cost", 0.0) or 0.0,
            "actual_trucking_cost": getattr(r, "actual_trucking_cost", 0.0) or 0.0,
            "quantity_from_job_costs": getattr(r, "quantity_from_job_costs", 0.0) or 0.0,
            "labor_hours": getattr(r, "labor_hours", 0.0) or 0.0,
            "equipment_hours": getattr(r, "equipment_hours", 0.0) or 0.0,
            "status": r.status,
            "alert": r.alert,
            "discrepancy_flag": r.discrepancy_flag,
        }
        for r in rows
    ]

    return summary, results


def get_or_fetch_projection_for_period(db, start_date: str, end_date: str) -> list:
    cache_key = _projection_cache_key(start_date, end_date)

    _, cached = get_projection_tracking(db, cache_key)
    if cached:
        logger.info("Projection DB HIT period=%s to %s key=%s records=%d", start_date, end_date, cache_key, len(cached))
        return cached

    logger.info("Projection DB MISS period=%s to %s key=%s -> fetching HCSS", start_date, end_date, cache_key)

    try:
        from projection_tracking.hcss_client import ProjectionHCSSClient
        from projection_tracking.analyzer import calculate_projections
        from projection_tracking.reporting import format_projection_items

        raw = ProjectionHCSSClient().fetch_projection_data(start_date=start_date, end_date=end_date)
        if not raw:
            logger.info("HCSS returned 0 projection records for period=%s to %s", start_date, end_date)
            return []

        results = calculate_projections(raw)
        dicts = format_projection_items(results)

        store_projection_tracking(db, cache_key, dicts)
        logger.info("Projection stored from HCSS period=%s to %s key=%s records=%d", start_date, end_date, cache_key, len(dicts))
        return dicts

    except Exception as exc:
        logger.error("Projection HCSS fetch failed period=%s to %s: %s", start_date, end_date, exc)
        return []


def get_or_fetch_projection_for_month(db, target_month: str) -> list:
    _, cached = get_projection_tracking(db, target_month)
    if cached:
        logger.info("Projection DB HIT month=%s records=%d", target_month, len(cached))
        return cached

    logger.info("Projection DB MISS month=%s -> fetching HCSS", target_month)

    try:
        from projection_tracking.hcss_client import ProjectionHCSSClient
        from projection_tracking.analyzer import calculate_projections
        from projection_tracking.reporting import format_projection_items

        raw = ProjectionHCSSClient().fetch_projection_data(month=target_month)
        if not raw:
            logger.info("HCSS returned 0 projection records for month=%s", target_month)
            return []

        results = calculate_projections(raw)
        dicts = format_projection_items(results)

        store_projection_tracking(db, target_month, dicts)
        logger.info("Projection stored from HCSS month=%s records=%d", target_month, len(dicts))
        return dicts

    except Exception as exc:
        logger.error("Projection HCSS fetch failed month=%s: %s", target_month, exc)
        return []


# ── POSTMORTEM TRACKING ──────────────────────────────────────────────────────

def _safe_float_pm(value: Any) -> float:
    """Safe float coerce for postmortem service (avoids name collision)."""
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def store_postmortem_data(
    db: Session,
    job_id: str,
    job_code: str,
    job_name: str,
    business_unit: str | None,
    totals: dict[str, Any],
    cost_codes: list[dict[str, Any]],
) -> int:
    """
    Upsert postmortem data for one job.

    Strategy:
        - INSERT or UPDATE the PostmortemJobSnapshot row (keyed on job_id).
        - DELETE all existing PostmortemCostCodeResult rows for this job,
          then bulk-INSERT the new ones.

    Returns the number of cost code rows stored.
    """
    now = datetime.now(timezone.utc)

    # ── Upsert job snapshot ────────────────────────────────────────────────
    snapshot = db.query(PostmortemJobSnapshot).filter(
        PostmortemJobSnapshot.job_id == job_id
    ).first()

    if snapshot:
        snapshot.job_code               = job_code
        snapshot.job_name               = job_name
        snapshot.business_unit          = business_unit
        snapshot.budgeted_all_cost      = _safe_float_pm(totals.get("budgeted_all_cost"))
        snapshot.expected_all_cost      = _safe_float_pm(totals.get("expected_all_cost"))
        snapshot.actual_cost            = _safe_float_pm(totals.get("actual_cost"))
        snapshot.variance               = _safe_float_pm(totals.get("variance"))
        snapshot.utilization_pct        = totals.get("utilization_pct")   # may be None
        snapshot.capped_cost_code_count = int(totals.get("capped_cost_code_count") or 0)
        snapshot.fetched_at             = now
        snapshot.updated_at             = now
    else:
        snapshot = PostmortemJobSnapshot(
            job_id               = job_id,
            job_code             = job_code,
            job_name             = job_name,
            business_unit        = business_unit,
            budgeted_all_cost    = _safe_float_pm(totals.get("budgeted_all_cost")),
            expected_all_cost    = _safe_float_pm(totals.get("expected_all_cost")),
            actual_cost          = _safe_float_pm(totals.get("actual_cost")),
            variance             = _safe_float_pm(totals.get("variance")),
            utilization_pct      = totals.get("utilization_pct"),
            capped_cost_code_count = int(totals.get("capped_cost_code_count") or 0),
            fetched_at           = now,
            created_at           = now,
            updated_at           = now,
        )
        db.add(snapshot)

    # Flush so FK constraint on cost code rows is satisfied
    db.flush()

    # ── Replace cost code rows ─────────────────────────────────────────────
    db.query(PostmortemCostCodeResult).filter(
        PostmortemCostCodeResult.job_id == job_id
    ).delete(synchronize_session=False)

    for cc in cost_codes:
        expected = _safe_float_pm(cc.get("expected_all_cost"))
        actual   = _safe_float_pm(cc.get("actual_cost"))
        # Ensure derived fields are always consistent even if the caller
        # omitted them (e.g. during partial updates or testing).
        util    = cc.get("utilization_pct")
        if util is None and expected > 0:
            util = round((actual / expected) * 100, 2)
        loss = _safe_float_pm(cc.get("loss_amount")) or round(max(0.0, actual - expected), 2)
        loss_pct = cc.get("loss_pct")
        if loss_pct is None and expected > 0 and loss > 0:
            loss_pct = round((loss / expected) * 100, 2)

        db.add(PostmortemCostCodeResult(
            id               = str(uuid.uuid4()),
            job_id           = job_id,
            cost_code_id     = cc.get("cost_code_id") or "",
            cost_code        = cc.get("cost_code") or "",
            description      = cc.get("description") or "",
            is_tm            = bool(cc.get("is_tm", False)),
            is_cap_expected  = bool(cc.get("is_cap_expected", True)),
            budgeted_qty     = _safe_float_pm(cc.get("budgeted_qty")),
            actual_qty       = _safe_float_pm(cc.get("actual_qty")),
            completion_pct   = cc.get("completion_pct"),           # None is valid
            budgeted_all_cost = _safe_float_pm(cc.get("budgeted_all_cost")),
            raw_formula      = cc.get("raw_formula"),              # None is valid
            expected_all_cost = expected,
            actual_cost      = actual,
            variance         = _safe_float_pm(cc.get("variance")),
            calc_method      = cc.get("calc_method") or "",
            utilization_pct  = util,
            loss_amount      = loss,
            loss_pct         = loss_pct,
            created_at       = now,
            updated_at       = now,
        ))

    db.commit()
    logger.info(
        "Postmortem stored: job_id=%s job_code=%s cost_codes=%d",
        job_id, job_code, len(cost_codes),
    )
    return len(cost_codes)


def get_postmortem_all_jobs(db: Session) -> list[PostmortemJobSnapshot]:
    """
    Return all PostmortemJobSnapshot rows ordered by job_code.
    Used by the /postmortem/list API endpoint.
    """
    return (
        db.query(PostmortemJobSnapshot)
        .order_by(PostmortemJobSnapshot.job_code)
        .all()
    )


def get_postmortem_job_snapshot(
    db: Session, job_id: str
) -> PostmortemJobSnapshot | None:
    """Return the PostmortemJobSnapshot for a single job, or None if not found."""
    return (
        db.query(PostmortemJobSnapshot)
        .filter(PostmortemJobSnapshot.job_id == job_id)
        .first()
    )


def get_postmortem_cost_codes(
    db: Session, job_id: str
) -> list[PostmortemCostCodeResult]:
    """
    Return all PostmortemCostCodeResult rows for a job, ordered by cost_code.
    Used by the /postmortem/detail API endpoint.
    """
    return (
        db.query(PostmortemCostCodeResult)
        .filter(PostmortemCostCodeResult.job_id == job_id)
        .order_by(
            PostmortemCostCodeResult.cost_code,
            PostmortemCostCodeResult.cost_code_id,
        )
        .all()
    )


def get_postmortem_job_by_code(
    db: Session, job_code: str
) -> PostmortemJobSnapshot | None:
    """Look up a PostmortemJobSnapshot by job_code (case-insensitive)."""
    return (
        db.query(PostmortemJobSnapshot)
        .filter(PostmortemJobSnapshot.job_code == job_code)
        .first()
    )


def purge_stale_postmortem_jobs(
    db: Session,
    active_job_ids: list[str],
) -> int:
    """
    Delete PostmortemJobSnapshot rows whose job_id is NOT in active_job_ids.

    PostmortemCostCodeResult rows cascade-delete automatically via the FK.

    Safety: if active_job_ids is empty, the purge is skipped entirely to
    prevent accidentally wiping the whole table when the HCSS fetch fails.

    Returns the number of snapshot rows deleted.
    """
    if not active_job_ids:
        logger.warning(
            "purge_stale_postmortem_jobs: active_job_ids is empty — skipping purge "
            "to prevent accidental full wipe"
        )
        return 0

    stale = (
        db.query(PostmortemJobSnapshot)
        .filter(PostmortemJobSnapshot.job_id.notin_(active_job_ids))
        .all()
    )

    if not stale:
        logger.info("purge_stale_postmortem_jobs: nothing to purge")
        return 0

    stale_codes = [s.job_code for s in stale]
    logger.info(
        "purge_stale_postmortem_jobs: removing %d stale snapshot(s): %s",
        len(stale),
        ", ".join(stale_codes[:20]) + ("…" if len(stale_codes) > 20 else ""),
    )

    for snap in stale:
        db.delete(snap)  # cascades to postmortem_cost_code_results

    db.commit()
    logger.info("purge_stale_postmortem_jobs: %d snapshot(s) purged", len(stale))
    return len(stale)
