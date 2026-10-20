"""
schedule_monitoring/data_gatherer.py
──────────────────────────────────────
Gathers ALL actual data needed for schedule monitoring.

Data strategy:
  ✅ DB only (cron keeps it fresh — no HCSS fallback):
      - Quantities       → quantity_tracking_results
      - Budget / costs   → budget_tracking_results
      - Labor / timelog  → timelog_timecard_results + timelog_verification_cache

  🌐 HCSS only (never stored in DB):
      - Job details      → POST /jobs/advanced  (name, start, est. completion)
      - Subcontractor    → fetch_job_subcontract_items per job

No baseline yet — this is the "actual" side only.
Baseline (6-week schedule from Google Drive) plugs in later via ScheduleAnalyzer.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

logger = logging.getLogger(__name__)


# ── Dataclasses ───────────────────────────────────────────────────────────────

@dataclass
class CostCodeSnapshot:
    """Actual data for one cost code within a job for the requested period."""
    cost_code_id: str
    cost_code: str
    description: str
    job_id: str
    job_code: str
    unit: str
    cost_type: str                    # "self_perform" | "subcontractor"

    # Quantities (from quantity_tracking_results)
    planned_quantity: float           # budget qty from HCSS cost codes
    installed_quantity: float         # installed in the period

    # Cost (from budget_tracking_results)
    budget_cost: float                # planned cost
    actual_cost: float                # actual spend in period
    labor_hours: float                # labor hours in period
    labor_cost: float
    equipment_cost: float
    material_cost: float
    subcontract_cost: float

    # Computed
    quantity_pct: float               # installed / planned * 100
    cost_pct: float                   # actual / budget * 100

    # Flags
    has_activity: bool                # any timecard activity in period
    seasonal_risk: str | None         # "asphalt" | "concrete" | None


@dataclass
class JobSnapshot:
    """All actual data rolled up per job for the requested period."""
    job_id: str
    job_code: str
    job_name: str
    business_unit: str

    # From HCSS job record
    start_date: str | None
    estimated_end_date: str | None

    # Rolled-up totals
    total_budget_cost: float
    total_actual_cost: float
    total_labor_hours: float
    total_planned_quantity: float
    total_installed_quantity: float

    # Percentages
    cost_pct: float                   # actual / budget * 100
    quantity_pct: float               # installed / planned * 100

    # Activity (from timelog DB)
    last_timecard_date: str | None
    active_days: int                  # distinct days with timecard activity
    days_since_activity: int          # days since last timecard (999 = no activity)

    # Detail rows
    cost_codes: list[CostCodeSnapshot] = field(default_factory=list)
    subcontractor_items: list[dict[str, Any]] = field(default_factory=list)


# ── Gatherer ──────────────────────────────────────────────────────────────────

class ScheduleDataGatherer:
    """
    Assembles a JobSnapshot list for any date range.
    Uses DB tables (always fresh via cron) for quantity/budget/timelog.
    Calls HCSS only for job details and subcontractor items.
    """

    def __init__(self, db=None) -> None:
        self._db = db          # caller can inject a session
        self._owns_db = False

    # ── Public ────────────────────────────────────────────────────────────────

    def gather_week(self, week_end_date: str | None = None) -> list[JobSnapshot]:
        """
        Gather data for the week ending on week_end_date (default: last Friday).
        """
        end_dt   = _resolve_week_end(week_end_date)
        start_dt = end_dt - timedelta(days=6)
        return self.gather_range(
            start_dt.strftime("%Y-%m-%d"),
            end_dt.strftime("%Y-%m-%d"),
        )

    def gather_range(self, start_date: str, end_date: str) -> list[JobSnapshot]:
        """Gather data for an arbitrary date range."""
        self._open_db()
        try:
            return self._gather(start_date, end_date)
        finally:
            self._close_db()

    # ── Internals ─────────────────────────────────────────────────────────────

    def _open_db(self) -> None:
        if self._db is None:
            from api.database import SessionLocal
            self._db = SessionLocal()
            self._owns_db = True

    def _close_db(self) -> None:
        if self._owns_db and self._db is not None:
            self._db.close()
            self._db = None
            self._owns_db = False

    def _gather(self, start: str, end: str) -> list[JobSnapshot]:
        dates = _date_list(start, end)

        # ── 1. Pull from DB tables ────────────────────────────────────────────
        qty_rows     = self._read_quantity_db(dates)
        budget_rows  = self._read_budget_db(dates)
        timelog_rows = self._read_timelog_db(dates)

        # Collect all job IDs seen across all three tables
        all_job_ids: set[str] = set()
        for r in qty_rows + budget_rows + timelog_rows:
            jid = r.get("job_id") or ""
            if jid:
                all_job_ids.add(jid)

        if not all_job_ids:
            logger.warning("No job data in DB for %s → %s", start, end)
            return []

        # ── 2. HCSS: job details + subcontractor items ────────────────────────
        job_details = self._fetch_job_details(list(all_job_ids))
        sub_data    = self._fetch_subcontractor_data(list(all_job_ids))

        # ── 3. Assemble ───────────────────────────────────────────────────────
        return self._assemble(
            start, end, dates,
            qty_rows, budget_rows, timelog_rows,
            job_details, sub_data,
        )

    # ── DB readers ────────────────────────────────────────────────────────────

    def _read_quantity_db(self, dates: list[str]) -> list[dict]:
        """Read quantity_tracking_results for every date in the range."""
        from api.db_services import get_quantity_tracking
        rows: list[dict] = []
        for d in dates:
            _, cached = get_quantity_tracking(self._db, d)
            rows.extend(cached)
        logger.info("Quantity DB: %d rows across %d date(s)", len(rows), len(dates))
        return rows

    def _read_budget_db(self, dates: list[str]) -> list[dict]:
        """Read budget_tracking_results for every date in the range."""
        from api.db_services import get_budget_tracking
        rows: list[dict] = []
        for d in dates:
            _, cached = get_budget_tracking(self._db, d)
            rows.extend(cached)
        logger.info("Budget DB: %d rows across %d date(s)", len(rows), len(dates))
        return rows

    def _read_timelog_db(self, dates: list[str]) -> list[dict]:
        """Read timelog_timecard_results for every date in the range."""
        from api.db_services import get_timelog_verification
        rows: list[dict] = []
        for d in dates:
            _, cached = get_timelog_verification(self._db, d)
            rows.extend(cached)
        logger.info("Timelog DB: %d rows across %d date(s)", len(rows), len(dates))
        return rows

    # ── HCSS fetchers (only for data NOT in DB) ───────────────────────────────

    def _fetch_job_details(self, job_ids: list[str]) -> dict[str, dict]:
        """POST /jobs/advanced — job name, start, estimated completion."""
        if not job_ids:
            return {}
        try:
            from payroll_verification.hcss_client import HCSSClient
            client = HCSSClient()
            raw = client._request(
                "POST",
                f"{client.heavyjob_base}/jobs/advanced",
                json={"jobIds": job_ids},
            )
            job_list = raw if isinstance(raw, list) else raw.get("results", [])
            details: dict[str, dict] = {}
            for job in job_list:
                jid = job.get("id")
                if jid:
                    code = job.get("code", "")
                    desc = job.get("description", "")
                    job["_name"] = f"{code} – {desc}".strip(" –") or jid[:8]
                    details[jid] = job
            logger.info("Job details: %d/%d fetched from HCSS", len(details), len(job_ids))
            return details
        except Exception as exc:
            logger.error("Job details HCSS fetch failed: %s", exc)
            return {}

    def _fetch_subcontractor_data(self, job_ids: list[str]) -> dict[str, list[dict]]:
        """Fetch subcontract items per job — not stored in DB."""
        result: dict[str, list[dict]] = {}
        if not job_ids:
            return result
        try:
            from payroll_verification.hcss_client import HCSSClient
            client = HCSSClient()
            for jid in job_ids:
                try:
                    items = client.fetch_job_subcontract_items(job_id=jid)
                    if items:
                        result[jid] = items
                except Exception:
                    pass  # subcontract data is optional, skip silently
        except Exception as exc:
            logger.error("Subcontractor HCSS fetch failed: %s", exc)
        logger.info("Subcontractor data: %d jobs with sub items", len(result))
        return result

    # ── Assembly ──────────────────────────────────────────────────────────────

    def _assemble(
        self,
        start: str,
        end: str,
        dates: list[str],
        qty_rows: list[dict],
        budget_rows: list[dict],
        timelog_rows: list[dict],
        job_details: dict[str, dict],
        sub_data: dict[str, list[dict]],
    ) -> list[JobSnapshot]:

        # ── Build keyed lookups ────────────────────────────────────────────────
        # quantity: (job_id, cost_code_id) → aggregated row
        qty_map: dict[tuple[str, str], dict] = {}
        for r in qty_rows:
            jid  = r.get("job_id", "")
            ccid = r.get("cost_code_id", "")
            if not jid or not ccid:
                continue
            key = (jid, ccid)
            if key in qty_map:
                qty_map[key]["installed_quantity"] = (
                    qty_map[key].get("installed_quantity", 0)
                    + r.get("installed_quantity", 0)
                )
            else:
                qty_map[key] = dict(r)

        # budget: (job_id, cost_code_id) → aggregated row
        budget_map: dict[tuple[str, str], dict] = {}
        for r in budget_rows:
            jid  = r.get("job_id", "")
            ccid = r.get("cost_code_id", "")
            if not jid or not ccid:
                continue
            key = (jid, ccid)
            if key in budget_map:
                for f in ("actual_cost", "labor_cost", "equipment_cost",
                          "material_cost", "subcontract_cost", "labor_hours"):
                    budget_map[key][f] = (
                        budget_map[key].get(f, 0) + r.get(f, 0)
                    )
            else:
                budget_map[key] = dict(r)

        # timelog: job_id → sorted list of active dates
        job_activity: dict[str, list[str]] = {}
        for r in timelog_rows:
            jid = r.get("job_id") or ""
            d   = r.get("date") or ""
            if jid and d:
                job_activity.setdefault(jid, [])
                if d not in job_activity[jid]:
                    job_activity[jid].append(d)

        # All unique job IDs
        all_job_ids: set[str] = set()
        for (jid, _) in qty_map:
            all_job_ids.add(jid)
        for (jid, _) in budget_map:
            all_job_ids.add(jid)
        for jid in job_activity:
            all_job_ids.add(jid)

        today    = date.today()
        snapshots: list[JobSnapshot] = []

        for jid in sorted(all_job_ids):
            detail   = job_details.get(jid, {})
            job_code = detail.get("code") or jid[:8]
            job_name = detail.get("_name") or job_code
            bu       = detail.get("businessUnitId") or "N/A"
            start_d  = (detail.get("startDate") or detail.get("createdDate") or start)[:10]
            est_end  = (detail.get("estimatedCompletionDate") or "")[:10] or None

            # All cost code IDs for this job
            cc_ids: set[str] = set()
            for (j2, cc) in qty_map:
                if j2 == jid:
                    cc_ids.add(cc)
            for (j2, cc) in budget_map:
                if j2 == jid:
                    cc_ids.add(cc)

            cc_snapshots: list[CostCodeSnapshot] = []
            for ccid in sorted(cc_ids):
                q = qty_map.get((jid, ccid), {})
                b = budget_map.get((jid, ccid), {})

                planned_qty   = float(q.get("planned_quantity", 0) or 0)
                installed_qty = float(q.get("installed_quantity", 0) or 0)
                qty_pct = round(installed_qty / planned_qty * 100, 2) if planned_qty > 0 else 0.0

                budget_cost = float(b.get("expected_budget", 0) or 0)
                actual_cost = float(b.get("actual_cost", 0) or 0)
                cost_pct    = round(actual_cost / budget_cost * 100, 2) if budget_cost > 0 else 0.0

                desc = (
                    q.get("description")
                    or b.get("cost_code_description")
                    or "No description"
                )
                cc_code = q.get("cost_code") or b.get("cost_code") or ccid[:8]

                seasonal = _seasonal_risk(desc.lower() + " " + cc_code.lower(), today)

                cc_snapshots.append(CostCodeSnapshot(
                    cost_code_id=ccid,
                    cost_code=cc_code,
                    description=desc,
                    job_id=jid,
                    job_code=job_code,
                    unit=q.get("unit") or "UNIT",
                    cost_type=q.get("cost_type") or "self_perform",
                    planned_quantity=planned_qty,
                    installed_quantity=installed_qty,
                    budget_cost=budget_cost,
                    actual_cost=actual_cost,
                    labor_hours=float(b.get("labor_hours", 0) or 0),
                    labor_cost=float(b.get("labor_cost", 0) or 0),
                    equipment_cost=float(b.get("equipment_cost", 0) or 0),
                    material_cost=float(b.get("material_cost", 0) or 0),
                    subcontract_cost=float(b.get("subcontract_cost", 0) or 0),
                    quantity_pct=qty_pct,
                    cost_pct=cost_pct,
                    has_activity=jid in job_activity,
                    seasonal_risk=seasonal,
                ))

            # Roll-up totals
            total_budget    = sum(cc.budget_cost for cc in cc_snapshots)
            total_actual    = sum(cc.actual_cost for cc in cc_snapshots)
            total_labor     = sum(cc.labor_hours for cc in cc_snapshots)
            total_planned_q = sum(cc.planned_quantity for cc in cc_snapshots)
            total_installed = sum(cc.installed_quantity for cc in cc_snapshots)

            cost_pct_job  = round(total_actual / total_budget * 100, 2) if total_budget > 0 else 0.0
            qty_pct_job   = round(total_installed / total_planned_q * 100, 2) if total_planned_q > 0 else 0.0

            activity_dates = sorted(job_activity.get(jid, []))
            last_tc        = activity_dates[-1] if activity_dates else None
            days_since     = (
                (today - datetime.fromisoformat(last_tc).date()).days
                if last_tc else 999
            )

            snapshots.append(JobSnapshot(
                job_id=jid,
                job_code=job_code,
                job_name=job_name,
                business_unit=bu,
                start_date=start_d,
                estimated_end_date=est_end,
                total_budget_cost=total_budget,
                total_actual_cost=total_actual,
                total_labor_hours=total_labor,
                total_planned_quantity=total_planned_q,
                total_installed_quantity=total_installed,
                cost_pct=cost_pct_job,
                quantity_pct=qty_pct_job,
                last_timecard_date=last_tc,
                active_days=len(activity_dates),
                days_since_activity=days_since,
                cost_codes=cc_snapshots,
                subcontractor_items=sub_data.get(jid, []),
            ))

        logger.info(
            "Assembled %d jobs / %d cost codes for %s → %s",
            len(snapshots),
            sum(len(j.cost_codes) for j in snapshots),
            start, end,
        )
        return snapshots


# ── Helpers ───────────────────────────────────────────────────────────────────

def _date_list(start: str, end: str) -> list[str]:
    dates, cur = [], datetime.fromisoformat(start)
    end_dt = datetime.fromisoformat(end)
    while cur <= end_dt:
        dates.append(cur.strftime("%Y-%m-%d"))
        cur += timedelta(days=1)
    return dates


def _seasonal_risk(text: str, today: date) -> str | None:
    asphalt_deadline  = date(today.year, 9, 30)
    concrete_deadline = date(today.year, 10, 15)
    if ("asphalt" in text or "paving" in text or "pave" in text) and today > asphalt_deadline:
        return "asphalt"
    if "concrete" in text and today > concrete_deadline:
        return "concrete"
    return None


def _resolve_week_end(week_end_date: str | None) -> date:
    if week_end_date:
        return datetime.fromisoformat(week_end_date).date()
    today = date.today()
    days_since_friday = (today.weekday() - 4) % 7
    return today - timedelta(days=days_since_friday)
