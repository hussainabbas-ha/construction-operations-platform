from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Literal

from payroll_verification.hcss_client import HCSSClient

logger = logging.getLogger(__name__)

Status = Literal["APPROVED", "FLAGGED", "REJECTED"]


@dataclass(frozen=True)
class TimecardResult:
    id: str
    date: str
    business_unit_id: str | None
    job_id: str | None
    job_code: str
    foreman_id: str | None
    foreman: str
    status: Status
    # One flat list of human-readable issue messages for this card's status:
    #   REJECTED -> the reject reasons, FLAGGED -> the warnings,
    #   APPROVED -> [] (or a note such as "Pre-approved in HeavyJob portal").
    # Cost-code names are embedded inline in the message text.
    reasons: list[str]
    # Job description (e.g. "Administration, Safety & Trucking")
    job_description: str = ""
    # HeavyJob portal sync status, set by the browser automation (not the verifier):
    #   pending | approved | rejected | failed | not_found | skipped
    portal_sync_status: str = "pending"
    portal_synced_at: str | None = None  # ISO timestamp of the last sync attempt

    @property
    def why(self) -> str:
        """Convenience one-line summary (not serialized to the API)."""
        return "; ".join(self.reasons) if self.reasons else "No issues"


def _cost_code_meta(timecard_detail: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Map timeCardCostCodeId -> readable cost-code info {cost_code_id, code, description}."""
    meta: dict[str, dict[str, Any]] = {}
    for cc in timecard_detail.get("costCodes", []) or []:
        tc_id = cc.get("timeCardCostCodeId")
        if tc_id:
            meta[tc_id] = {
                "cost_code_id": cc.get("costCodeId"),
                "code": cc.get("costCodeCode") or (tc_id[:8] if tc_id else ""),
                "description": cc.get("costCodeDescription") or "",
            }
    return meta


def _parse_hcss_datetime(dt_str: str | None) -> datetime | None:
    if not dt_str:
        return None
    s = dt_str.replace("Z", "")
    if "." in s:
        s = s.split(".", 1)[0]
    try:
        return datetime.strptime(s, "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return None


def _submission_datetime(timecard_detail: dict[str, Any]) -> datetime | None:
    return _parse_hcss_datetime(timecard_detail.get("lockedDateTime")) or _parse_hcss_datetime(
        timecard_detail.get("lastModifiedDateTime")
    )

def _late_submission_flag(timecard_date_str: str, submitted_dt: datetime | None) -> bool:
    if not submitted_dt:
        return False
    tc_dt = datetime.strptime(timecard_date_str, "%Y-%m-%d")
    weekday = tc_dt.weekday()  # Mon=0 … Sun=6
    if weekday == 4:    # Friday → Monday 1 PM
        deadline = tc_dt + timedelta(days=3, hours=13)
    elif weekday == 5:  # Saturday → Monday 1 PM
        deadline = tc_dt + timedelta(days=2, hours=13)
    else:               # All other weekdays → next day 1 PM
        deadline = tc_dt + timedelta(days=1, hours=13)
    return submitted_dt > deadline


def _sum_labor_hours(timecard_detail: dict[str, Any]) -> tuple[float, dict[str, float]]:
    total = 0.0
    hours_by_tc_cost_code: dict[str, float] = {}
    for emp in timecard_detail.get("employees", []) or []:
        entries = (
            (emp.get("regularHours") or [])
            + (emp.get("overtimeHours") or [])
            + (emp.get("doubleOvertimeHours") or [])
        )
        for h in entries:
            hrs = float(h.get("hours") or 0)
            total += hrs
            tc_cc_id = h.get("timeCardCostCodeId")
            if tc_cc_id:
                hours_by_tc_cost_code[tc_cc_id] = hours_by_tc_cost_code.get(tc_cc_id, 0.0) + hrs
    return total, hours_by_tc_cost_code


def _employee_hour_flags(timecard_detail: dict[str, Any]) -> list[str]:
    """
    FLAG 5 — Employee hours > 12 in a day.
    FLAG 6 — Employee listed on timecard with 0 total hours.

    Returns a list of flag strings (empty if no issues found).
    Each employee is identified by their description/name field; falls back
    to a short ID slice when no name is available.
    """
    flags: list[str] = []
    for emp in timecard_detail.get("employees", []) or []:
        # Resolve a human-readable name for the flag message
        name = (
            emp.get("employeeDescription")
            or emp.get("description")
            or emp.get("name")
            or emp.get("employeeName")
            or emp.get("employeeCode")
            or (emp.get("employeeId") or "unknown")[:8]
        )

        entries = (
            (emp.get("regularHours") or [])
            + (emp.get("overtimeHours") or [])
            + (emp.get("doubleOvertimeHours") or [])
        )
        total_hrs = sum(float(h.get("hours") or 0) for h in entries)

        if total_hrs == 0.0:
            flags.append(f"Employee with 0 hours: {name}")
        elif total_hrs > 12.0:
            flags.append(f"Employee over 12 hours ({total_hrs:.1f} hrs): {name}")

    return flags


def _timecard_cost_code_quantities(timecard_detail: dict[str, Any]) -> tuple[dict[str, float], bool]:
    qty: dict[str, float] = {}
    notes_present = False
    for item in timecard_detail.get("costCodes", []) or []:
        tc_id = item.get("timeCardCostCodeId")
        if tc_id:
            try:
                qty_val = float(item.get("quantity") or 0)
            except (TypeError, ValueError):
                qty_val = 0.0
            qty[tc_id] = qty_val
        for key in ("publicNotes", "privateNotes", "notes", "note"):
            val = item.get(key)
            if isinstance(val, str) and val.strip():
                notes_present = True
                break
    return qty, notes_present


def _diary_has_meaningful_text(diary_obj: dict[str, Any]) -> bool:
    for key in ("note", "notes", "text", "diaryText", "description", "comments", "comment"):
        val = diary_obj.get(key)
        if isinstance(val, str) and val.strip():
            return True
    return False


def verify_payroll_date(
    *,
    target_date: str,
    business_unit_id: str | None = None,
    client: HCSSClient | None = None,
) -> list[TimecardResult]:
    """
    Verify all timecards for a single date against the 7 time log rules.

    REJECT (hard failures):
      1. Missing labor hours
      2. Missing quantities — cost code has hours but quantity = 0
      3. Missing diary entry — no diary text AND no cost-code notes

    FLAG (soft warnings, only checked when not rejected):
      1. Quantity without labor — cost code has quantity but hours = 0
      2. No photos attached
      3. Late submission
      4. Missing subcontractor info
    """
    c = client or HCSSClient()
    bu_filter = (business_unit_id or "").strip() or None

    logger.info("Starting verification: date=%s bu=%s", target_date, bu_filter or "all")

    try:
        timecard_summaries = c.fetch_timecards(
            start_date=target_date,
            end_date=target_date,
            business_unit_id=bu_filter,
        )
    except Exception:
        logger.exception("Failed to fetch timecards for date=%s", target_date)
        raise

    logger.info("Processing %d timecard(s) for %s", len(timecard_summaries), target_date)

    diaries_cache: dict[tuple[str | None, str | None, str | None], list[dict[str, Any]]] = {}
    photos_cache: dict[tuple[str | None, str | None, str | None], bool] = {}
    job_subcontract_cache: dict[str, list[dict[str, Any]]] = {}
    subcontract_work_cache: dict[tuple[str | None, str | None, str | None], list[dict[str, Any]]] = {}

    results: list[TimecardResult] = []

    for tc_summary in timecard_summaries:
        tc_id = tc_summary.get("id")

        logger.info("TC_SUMMARY_KEYS: %s", list(tc_summary.keys()))
        logger.info("TC_SUMMARY_SAMPLE: %s", {k: tc_summary.get(k) for k in ["isLocked", "locked", "lockedDateTime", "approvalStatus", "status", "isApproved"]})

        if not tc_id:
            logger.warning("Skipping timecard summary with no id: %s", tc_summary)
            continue
                    # ── HCSS PRE-APPROVAL CHECK ─────────────────────────────────────
        # If supervisor already approved this timecard in HeavyJob portal,
        # skip all 7 rules and mark as APPROVED immediately.
        # This also skips fetch_timecard_detail() saving one HTTP call.
        is_hcss_approved = tc_summary.get("isApproved") is True

        if is_hcss_approved:
            job_id_pre     = tc_summary.get("jobId")
            foreman_id_pre = tc_summary.get("foremanId")
            bu_id_pre      = tc_summary.get("businessUnitId")

            # Fetch detail for job/foreman names with retry on 429
            import time
            job_code_pre = "Unknown"
            foreman_pre  = "Unknown"
            job_desc_pre = ""

            for attempt in range(3):  # retry up to 3 times
                try:
                    detail_pre   = c.fetch_timecard_detail(tc_id)
                    job_code_pre = detail_pre.get("jobCode", "Unknown")
                    job_desc_pre = detail_pre.get("jobDescription", "") or ""
                    foreman_pre  = detail_pre.get("foremanDescription", "Unknown")
                    bu_id_pre    = detail_pre.get("businessUnitId", bu_id_pre)
                    break  # success — exit retry loop
                except Exception as e:
                    if "429" in str(e):
                        wait = (attempt + 1) * 5  # 5s, 10s, 15s
                        logger.warning(
                            "Rate limited fetching detail for pre-approved tc=%s "
                            "— waiting %ds before retry (attempt %d/3)",
                            tc_id[:8], wait, attempt + 1,
                        )
                        time.sleep(wait)
                    else:
                        logger.warning(
                            "Could not fetch detail for pre-approved tc=%s: %s",
                            tc_id[:8], e,
                        )
                        break  # non-429 error, don't retry

            logger.info(
                "✓ PRE-APPROVED: tc=%s | job=%s | foreman=%s | skipping all 7 rules",
                tc_id[:8], job_code_pre, foreman_pre,
            )

            results.append(
                TimecardResult(
                    id=tc_id,
                    date=target_date,
                    business_unit_id=bu_id_pre,
                    job_id=job_id_pre,
                    job_code=job_code_pre,
                    foreman_id=foreman_id_pre,
                    foreman=foreman_pre,
                    status="APPROVED",
                    reasons=["Pre-approved in HeavyJob portal"],
                    job_description=job_desc_pre,
                )
            )
            continue  # skip all 7 rule checks
        # ── END HCSS PRE-APPROVAL CHECK ─────────────────────────────────

        # ------------------------------------------------------------------ #
        # Fetch timecard detail
        # ------------------------------------------------------------------ #
        try:
            detail = c.fetch_timecard_detail(tc_id)
        except Exception:
            logger.exception("Failed to fetch detail for timecard id=%s — skipping", tc_id)
            continue

        job_id   = detail.get("jobId")   or tc_summary.get("jobId")
        job_code = detail.get("jobCode") or "Unknown"
        job_description = detail.get("jobDescription") or ""
        foreman_id = detail.get("foremanId") or tc_summary.get("foremanId")
        foreman    = detail.get("foremanDescription") or "Unknown"
        bu_id      = detail.get("businessUnitId") or tc_summary.get("businessUnitId")

        cc_meta = _cost_code_meta(detail)

        def _codes(tc_cc_ids: list[str]) -> str:
            """Readable cost-code codes for the given timecard-cost-code ids."""
            return ", ".join(
                (cc_meta.get(i) or {}).get("code") or (i[:8] if i else "")
                for i in tc_cc_ids
            )

        # Hard failures (cause REJECTED) and soft warnings (cause FLAGGED) are
        # collected separately so we can set status, but only ONE of them ends up
        # in the card's flat `reasons` list (status tells which).
        rejects: list[str] = []
        warns:   list[str] = []

        # ------------------------------------------------------------------ #
        # REJECT 1 — Missing Labor Hours
        # ------------------------------------------------------------------ #
        total_labor, hours_by_tc_cc = _sum_labor_hours(detail)
        if total_labor <= 0:
            rejects.append("Missing labor hours")

        # ------------------------------------------------------------------ #
        # REJECT 2 — Missing Quantities (cost code has hours but quantity = 0)
        # ------------------------------------------------------------------ #
        qty_by_tc_cc, tc_notes_present = _timecard_cost_code_quantities(detail)
        missing_qty_ids = [
            tc_cc_id for tc_cc_id, hrs in hours_by_tc_cc.items()
            if hrs > 0 and qty_by_tc_cc.get(tc_cc_id, 0.0) <= 0
        ]
        if missing_qty_ids:
            rejects.append(f"Missing quantities for cost codes: {_codes(missing_qty_ids)}")

        # ------------------------------------------------------------------ #
        # REJECT 3 — Missing Diary Entry (no diary text AND no cost-code notes)
        # ------------------------------------------------------------------ #
        diary_ok = tc_notes_present  # cost-code notes satisfy the requirement
        if not diary_ok and bu_id:
            diary_key = (bu_id, job_id, foreman_id)
            diaries = diaries_cache.get(diary_key)
            if diaries is None:
                try:
                    diaries = c.fetch_diaries(
                        business_unit_id=bu_id,
                        job_ids=[job_id] if job_id else None,
                        foreman_ids=[foreman_id] if foreman_id else None,
                        start_date=target_date,
                        end_date=target_date,
                    )
                except Exception:
                    logger.exception(
                        "Failed to fetch diaries for tc=%s job=%s foreman=%s — treating as missing",
                        tc_id[:8], job_code, foreman,
                    )
                    diaries = []
                diaries_cache[diary_key] = diaries
            diary_ok = bool(diaries and any(_diary_has_meaningful_text(d) for d in diaries))
        if not diary_ok:
            rejects.append("Missing diary entry")

        # ------------------------------------------------------------------ #
        # Soft checks — only run when no hard failures
        # ------------------------------------------------------------------ #
        if not rejects:

            # FLAG 1 — Quantity Without Labor
            for tc_cc_id, qty_val in qty_by_tc_cc.items():
                if qty_val > 0 and hours_by_tc_cc.get(tc_cc_id, 0.0) <= 0:
                    warns.append(f"Quantity without labor (CC: {_codes([tc_cc_id])})")

            # FLAG 2 — No Photos Attached
            # Scoped to THIS foreman so it matches the per-card view in the portal
            # (photos on another foreman's card for the same job no longer count).
            photos_key = (bu_id, job_id, foreman_id)
            has_photos = photos_cache.get(photos_key)
            if has_photos is None:
                if bu_id:
                    try:
                        photos = c.fetch_attachments_advanced(
                            business_unit_id=bu_id,
                            job_ids=[job_id] if job_id else None,
                            foreman_ids=[foreman_id] if foreman_id else None,
                            start_date=target_date,
                            end_date=target_date,
                            file_type="photos",
                        )
                        has_photos = len(photos) > 0
                    except Exception:
                        logger.exception(
                            "Failed to fetch photos for tc=%s job=%s — treating as missing",
                            tc_id[:8], job_code,
                        )
                        has_photos = False
                else:
                    logger.warning(
                        "No businessUnitId for tc=%s job=%s — cannot check photos, treating as missing",
                        tc_id[:8], job_code,
                    )
                    has_photos = False
                photos_cache[photos_key] = has_photos
            if not has_photos:
                warns.append("No photos attached")

            # FLAG 3 — Late Submission
            if _late_submission_flag(target_date, _submission_datetime(detail)):
                warns.append("Late submission")

            # FLAG 4 — Missing Subcontractor Info
            if job_id:
                job_subs = job_subcontract_cache.get(job_id)
                if job_subs is None:
                    try:
                        job_subs = c.fetch_job_subcontract_items(job_id=job_id)
                    except Exception:
                        logger.exception(
                            "Failed to fetch subcontract items for job=%s tc=%s — skipping flag",
                            job_code, tc_id[:8],
                        )
                        job_subs = []
                    job_subcontract_cache[job_id] = job_subs
                if job_subs:
                    sw_key = (bu_id, job_id, foreman_id)
                    txns = subcontract_work_cache.get(sw_key)
                    if txns is None:
                        try:
                            txns = c.fetch_subcontract_work_transactions_advanced(
                                business_unit_id=bu_id,
                                job_ids=[job_id],
                                foreman_ids=[foreman_id] if foreman_id else None,
                                start_date=target_date,
                                end_date=target_date,
                            )
                        except Exception:
                            logger.exception(
                                "Failed to fetch subcontract transactions for job=%s tc=%s — skipping flag",
                                job_code, tc_id[:8],
                            )
                            txns = []
                        subcontract_work_cache[sw_key] = txns
                    if not txns:
                        warns.append("Missing subcontractor info")

            # FLAG 5 — Employee over 12 hours / Employee with 0 hours
            warns.extend(_employee_hour_flags(detail))

        # ------------------------------------------------------------------ #
        # Final status + single flat reasons list
        # ------------------------------------------------------------------ #
        tc_status: Status
        if rejects:
            tc_status = "REJECTED"
            card_reasons = rejects
        elif warns:
            tc_status = "FLAGGED"
            card_reasons = warns
        else:
            tc_status = "APPROVED"
            card_reasons = []

        results.append(
            TimecardResult(
                id=tc_id,
                date=target_date,
                business_unit_id=bu_id,
                job_id=job_id,
                job_code=job_code,
                foreman_id=foreman_id,
                foreman=foreman,
                status=tc_status,
                reasons=card_reasons,
                job_description=job_description,
            )
        )

    approved = sum(1 for r in results if r.status == "APPROVED")
    flagged  = sum(1 for r in results if r.status == "FLAGGED")
    rejected = sum(1 for r in results if r.status == "REJECTED")

    logger.info(
        "Verification complete: date=%s total=%d approved=%d flagged=%d rejected=%d",
        target_date, len(results), approved, flagged, rejected,
    )
    return results

def verify_payroll_range(
    *,
    start_date: str,
    end_date: str,
    business_unit_id: str | None = None,
    client: HCSSClient | None = None,
) -> list[TimecardResult]:
    """
    Verify all timecards across an inclusive date range.

    Iterates each date from *start_date* to *end_date* and aggregates the
    results into a single flat list.  Each :class:`TimecardResult` carries its
    own ``date`` field so callers can group by date if needed.
    """
    start = date.fromisoformat(start_date)
    end   = date.fromisoformat(end_date)

    c = client or HCSSClient()
    all_results: list[TimecardResult] = []

    current = start
    while current <= end:
        day_str = current.strftime("%Y-%m-%d")
        logger.info("Range verification: processing date=%s", day_str)
        try:
            day_results = verify_payroll_date(
                target_date=day_str,
                business_unit_id=business_unit_id,
                client=c,
            )
        except Exception:
            logger.exception("Range verification: failed for date=%s — skipping", day_str)
            day_results = []
        all_results.extend(day_results)
        current += timedelta(days=1)

    logger.info(
        "Range verification complete: %s → %s total=%d",
        start_date, end_date, len(all_results),
    )
    return all_results
