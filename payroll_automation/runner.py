"""
Drive Approve/Reject on the HeavyJob portal from payroll verification results.

Entry point: ``await run_portal_actions(results, target_date, dry_run=...)``.

Can also be run standalone for testing:
    python -m payroll_automation.runner 2026-06-17           # dry-run
    python -m payroll_automation.runner 2026-06-17 --live     # real clicks
"""
from __future__ import annotations

import asyncio
import logging
import os
from collections import defaultdict
from datetime import date
from typing import Any, Iterable

logger = logging.getLogger(__name__)

# status (from payroll_verification.verifier) -> portal action
_ACTION = {"APPROVED": "approve", "FLAGGED": "approve", "REJECTED": "reject"}


def _to_mmddyyyy(iso_date: str) -> str:
    return date.fromisoformat(iso_date).strftime("%m/%d/%Y")


def _bool_env(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() == "true"


async def run_portal_actions(
    results: Iterable[Any],
    target_date: str,
    *,
    dry_run: bool | None = None,
    headed: bool | None = None,
) -> list[dict[str, Any]]:
    """
    For each verification result, Approve/Reject the matching portal timecard.

    *results* are ``payroll_verification.verifier.TimecardResult`` objects.
    *target_date* is ISO ``YYYY-MM-DD``.

    Returns a list of per-timecard action records (also useful for logging/email).
    """
    from payroll_automation.portal import HeavyJobPortal

    if dry_run is None:
        dry_run = _bool_env("PORTAL_AUTOMATION_DRY_RUN", True)
    if headed is None:
        headed = _bool_env("PORTAL_AUTOMATION_HEADED", False)

    user = os.getenv("HJ_PORTAL_USER")
    password = os.getenv("HJ_PORTAL_PASS")
    if not user or not password:
        raise RuntimeError("HJ_PORTAL_USER / HJ_PORTAL_PASS not set in environment")

    results = list(results)
    # group by job_code so we filter the portal once per job
    by_job: dict[str, list[Any]] = defaultdict(list)
    for r in results:
        by_job[r.job_code].append(r)

    date_us = _to_mmddyyyy(target_date)
    mode = "DRY-RUN" if dry_run else "LIVE"
    try:
        retries = max(0, int(os.getenv("PORTAL_AUTOMATION_RETRIES", "2")))
    except ValueError:
        retries = 2
    logger.info("Portal automation [%s]: date=%s jobs=%d timecards=%d retries=%d",
                mode, target_date, len(by_job), len(results), retries)

    actions: list[dict[str, Any]] = []

    async with HeavyJobPortal(user, password, headed=headed) as portal:
        await portal.login()

        for job_code, job_results in by_job.items():
            if job_code in (None, "", "Unknown"):
                for r in job_results:
                    actions.append(_record(r, "skipped", "no usable job_code", dry_run))
                    logger.warning("Skipping tc=%s — no usable job_code", r.id[:8])
                continue

            try:
                await portal.filter_to(date_mmddyyyy=date_us, job_code=job_code)
            except Exception as exc:
                logger.exception("Filter failed for job=%s", job_code)
                for r in job_results:
                    actions.append(_record(r, "error", f"filter failed: {exc}", dry_run))
                continue

            for r in job_results:
                action = _ACTION.get(r.status)
                if action is None:
                    actions.append(_record(r, "skipped", f"unknown status {r.status}", dry_run))
                    continue

                # Attempt match + action with auto-retry. A failed attempt
                # (not_found / click error) is retried after re-filtering the job
                # for a clean page state. Success / would_* stops immediately.
                rec = None
                for attempt in range(retries + 1):
                    if attempt > 0:
                        logger.warning(
                            "Retry %d/%d: job=%s foreman=%s (prev=%s)",
                            attempt, retries, job_code, r.foreman, rec["action"] if rec else "?",
                        )
                        try:
                            await portal.filter_to(date_mmddyyyy=date_us, job_code=job_code)
                        except Exception:
                            logger.exception("Re-filter failed on retry for job=%s", job_code)
                            continue
                    rec = await _try_match_and_act(portal, r, job_code, date_us, dry_run, action)
                    if rec["action"] not in ("not_found", "error"):
                        break  # success (or would_* in dry-run)
                actions.append(rec)

    _log_summary(actions, mode)
    return actions


async def _try_match_and_act(portal, r, job_code: str, date_us: str, dry_run: bool, action: str) -> dict:
    """One attempt: match the foreman's card, then approve/reject (or log in dry-run)."""
    try:
        matched = await portal.open_card_for_foreman(
            foreman=r.foreman, job_code=job_code, date_mmddyyyy=date_us,
        )
    except Exception as exc:
        logger.exception("Match failed for job=%s foreman=%s", job_code, r.foreman)
        return _record(r, "error", f"match failed: {exc}", dry_run)

    if not matched:
        logger.warning("No portal match: job=%s foreman=%s", job_code, r.foreman)
        return _record(r, "not_found", f"no portal timecard for foreman={r.foreman}", dry_run)

    if dry_run:
        logger.info("[DRY-RUN] would %s: job=%s foreman=%s (%s)",
                    action.upper(), job_code, r.foreman, r.why)
        return _record(r, f"would_{action}", r.why, dry_run)

    # Idempotency: if the card is already in the target state on the portal,
    # don't click again (approved cards have no Approve button → would time out).
    desired_state = "approved" if action == "approve" else "rejected"
    try:
        current_state = await portal.open_card_state()
    except Exception:
        current_state = ""
    if current_state == desired_state:
        logger.info("Already %s on portal: job=%s foreman=%s — skipping click",
                    current_state, job_code, r.foreman)
        return _record(r, action, f"{r.why} (already {current_state})", dry_run)

    try:
        if action == "approve":
            await portal.approve_open_card()
        else:
            await portal.reject_open_card()
        logger.info("%s: job=%s foreman=%s", action.upper(), job_code, r.foreman)
        return _record(r, action, r.why, dry_run)
    except Exception as exc:
        logger.exception("Action %s failed for job=%s foreman=%s", action, job_code, r.foreman)
        return _record(r, "error", f"{action} failed: {exc}", dry_run)


def _record(r: Any, action: str, detail: str, dry_run: bool) -> dict[str, Any]:
    return {
        "timecard_id": r.id,
        "job_code": r.job_code,
        "foreman": r.foreman,
        "status": r.status,
        "action": action,
        "detail": detail,
        "dry_run": dry_run,
    }


def _log_summary(actions: list[dict[str, Any]], mode: str) -> None:
    counts: dict[str, int] = defaultdict(int)
    for a in actions:
        counts[a["action"]] += 1
    logger.info("Portal automation [%s] summary: %s", mode, dict(counts))


# ── standalone test harness ───────────────────────────────────────────────
def _main() -> None:
    import argparse
    import json

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(description="Run HeavyJob portal Approve/Reject for a date")
    parser.add_argument("date", help="target date YYYY-MM-DD")
    parser.add_argument("--live", action="store_true", help="perform real clicks (default: dry-run)")
    parser.add_argument("--headed", action="store_true", help="show the browser")
    parser.add_argument("--job", help="only act on this one job code (e.g. 25048) — for quick demos")
    parser.add_argument("--first", action="store_true",
                        help="only the first timecard of the selected job (single click)")
    args = parser.parse_args()

    from payroll_verification.verifier import verify_payroll_date

    results = verify_payroll_date(target_date=args.date)
    print(f"Verification: {len(results)} timecard(s) for {args.date}")
    for r in results:
        print(f"  - {r.job_code} | {r.foreman} | {r.status} | {r.why}")

    # Demo helpers: narrow to a single job (and optionally a single timecard).
    if args.job:
        results = [r for r in results if r.job_code == args.job]
        if not results:
            print(f"\nNo timecard for job {args.job} on {args.date}. "
                  f"Available jobs: {sorted({r.job_code for r in verify_payroll_date(target_date=args.date)})}")
            return
        if args.first:
            results = results[:1]
        print(f"\n>>> DEMO: acting on job {args.job} only — "
              f"{len(results)} timecard(s): "
              + ", ".join(f"{r.foreman} [{r.status}->{_ACTION.get(r.status, '?')}]" for r in results))

    actions = asyncio.run(run_portal_actions(
        results, args.date, dry_run=not args.live, headed=args.headed,
    ))
    print("\n=== ACTIONS ===")
    print(json.dumps(actions, indent=2))


if __name__ == "__main__":
    _main()
