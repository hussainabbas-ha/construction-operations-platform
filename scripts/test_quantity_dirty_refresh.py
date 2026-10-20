"""
test_quantity_dirty_refresh.py
--------------------------------------------------------------------------------
Prove whether quantity dirty-date refresh via HCSS adjustments?modifiedSince
can fix stale quantity_test rows (without touching production tables/cron).

Flow:
  1. Call GET /v1/costTypeQuantities/adjustments?modifiedSince=...
  2. Detailed report of every adjustment row (job, cost code, transaction date,
     quantity, lastModified, revision)
  3. Collect dirty transaction dates in [--min-date, --max-date]
  4. Snapshot quantity_test SUM for watchlist cost codes BEFORE refresh
  5. Re-fetch progress for each dirty day and overwrite quantity_test for that day
  6. Snapshot AFTER refresh + write JSON report

USAGE:
  python scripts/test_quantity_dirty_refresh.py
  python scripts/test_quantity_dirty_refresh.py --since-hours 48
  python scripts/test_quantity_dirty_refresh.py --modified-since 2026-07-19T00:00:00Z
  python scripts/test_quantity_dirty_refresh.py --dry-run
  python scripts/test_quantity_dirty_refresh.py --min-date 2026-05-01 --max-date 2026-07-20

Does NOT touch quantity_tracking_* or cron.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from dotenv import load_dotenv

load_dotenv(_REPO_ROOT / ".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)-8s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("quantity_dirty_refresh")

from payroll_verification.hcss_client import HCSSClient  # noqa: E402
from api.database import SessionLocal, create_tables, QuantityTest  # noqa: E402
from api.db_services import (  # noqa: E402
    get_quantity_test_days_present,
    store_quantity_test,
    sum_quantity_test_for_period,
)

# Import helpers from sibling populate script (not a package)
import importlib.util  # noqa: E402

_pop_spec = importlib.util.spec_from_file_location(
    "populate_quantity_test",
    _REPO_ROOT / "scripts" / "populate_quantity_test.py",
)
_pop = importlib.util.module_from_spec(_pop_spec)
assert _pop_spec.loader is not None
_pop_spec.loader.exec_module(_pop)
build_day_rows = _pop.build_day_rows
fetch_active_jobs = _pop.fetch_active_jobs
fetch_cost_code_meta = _pop.fetch_cost_code_meta
progress_for_day = _pop.progress_for_day

_ADJUSTMENTS = (
    "https://api.hcssapps.com/heavyjob/api/v1/costTypeQuantities/adjustments"
)

# Discrepancies reported by user (May 1 -> Jul 20 compare)
WATCHLIST = [
    {"job_code": "25006", "cost_code": "80711111", "hcss": 26557.0, "dashboard": 25257.0},
    {"job_code": "25006", "cost_code": "80011111B", "hcss": 11326.0, "dashboard": 11132.0},
    {"job_code": "25006", "cost_code": "58011111D", "hcss": 600.0, "dashboard": 565.0},
    {"job_code": "25069C", "cost_code": "14511111C", "hcss": 565.9, "dashboard": 545.55},
    {"job_code": "26004", "cost_code": "61340010A", "hcss": 19.5, "dashboard": 3.0},
    {"job_code": "25060CONST", "cost_code": "40011111", "hcss": 51.0, "dashboard": 34.0},
    {"job_code": "25060CONST", "cost_code": "24011111A", "hcss": 185.97, "dashboard": 182.97},
    {"job_code": "25048", "cost_code": "12011111C", "hcss": 5.06, "dashboard": 4.06},
    {"job_code": "25006", "cost_code": "58511111C", "hcss": 7.95, "dashboard": 7.70},
    {"job_code": "25006", "cost_code": "5111111", "hcss": 9.24, "dashboard": 9.09},
    {"job_code": "25060CONST", "cost_code": "211111I", "hcss": 7.71, "dashboard": 7.61},
    {"job_code": "25006", "cost_code": "60011111B", "hcss": 0.05, "dashboard": 0.0},
    {"job_code": "25006", "cost_code": "1011111", "hcss": 2.38, "dashboard": 2.36},
]

BOLD = "\033[1m"
GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
CYAN = "\033[96m"
RESET = "\033[0m"


def _sf(val: Any) -> float:
    try:
        return float(val or 0)
    except (TypeError, ValueError):
        return 0.0


def _parse_day(val: Any) -> str | None:
    if val is None:
        return None
    s = str(val).strip()
    if not s:
        return None
    # "2019-02-28" or "2019-02-28T03:01:15Z"
    return s[:10]


def _paginate_get(client: HCSSClient, url: str, params: dict) -> list[dict]:
    rows: list[dict] = []
    cursor = None
    page = 0
    while True:
        p = dict(params)
        if cursor:
            p["cursor"] = cursor
        raw = client._request("GET", url, params=p)
        batch, next_cursor = client._extract_results_and_cursor(raw)
        page += 1
        rows.extend(batch)
        logger.info(
            "adjustments page %d: %d row(s) (total=%d)",
            page, len(batch), len(rows),
        )
        if not next_cursor:
            break
        cursor = next_cursor
    return rows


def fetch_adjustments(
    client: HCSSClient, modified_since: datetime
) -> list[dict]:
    # HCSS accepts yyyy-MM-ddThh:mm:ssZ
    if modified_since.tzinfo is None:
        ms = modified_since.replace(tzinfo=timezone.utc)
    else:
        ms = modified_since.astimezone(timezone.utc)
    ms_str = ms.strftime("%Y-%m-%dT%H:%M:%SZ")
    logger.info("Calling adjustments?modifiedSince=%s", ms_str)
    return _paginate_get(
        client,
        _ADJUSTMENTS,
        {"modifiedSince": ms_str, "limit": 1000},
    )


def snapshot_watchlist(
    db, start: str, end: str
) -> dict[tuple[str, str], dict]:
    """(job_code, cost_code) -> period-summed row from quantity_test."""
    items = sum_quantity_test_for_period(db, start, end)
    out: dict[tuple[str, str], dict] = {}
    for item in items:
        key = (
            (item.get("job_code") or "").upper(),
            (item.get("cost_code") or "").upper(),
        )
        out[key] = item
    return out


def daily_breakdown_for_codes(
    db, start: str, end: str, keys: set[tuple[str, str]]
) -> dict[str, list[dict]]:
    """Per watchlist key, list of daily installed amounts in range (non-zero only)."""
    rows = (
        db.query(QuantityTest)
        .filter(
            QuantityTest.tracking_date >= start,
            QuantityTest.tracking_date <= end,
        )
        .all()
    )
    by_key: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        key = ((r.job_code or "").upper(), (r.cost_code or "").upper())
        if key not in keys:
            continue
        qty = float(r.installed_quantity or 0)
        if qty == 0:
            continue
        label = f"{key[0]}|{key[1]}"
        by_key[label].append({
            "date": r.tracking_date,
            "installed": qty,
        })
    for label in by_key:
        by_key[label].sort(key=lambda x: x["date"])
    return dict(by_key)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Discover quantity dirty dates via adjustments?modifiedSince and refresh quantity_test"
    )
    parser.add_argument(
        "--modified-since",
        default="",
        help="ISO datetime for modifiedSince (default: now - since-hours)",
    )
    parser.add_argument(
        "--since-hours",
        type=float,
        default=48.0,
        help="If --modified-since omitted, use now minus this many hours (default 48)",
    )
    parser.add_argument("--min-date", default="2026-05-01")
    parser.add_argument("--max-date", default="2026-07-20")
    parser.add_argument(
        "--delay", type=float, default=0.35, help="Delay between progress calls"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Discover + report only; do not overwrite quantity_test",
    )
    parser.add_argument(
        "--out",
        default="",
        help="Write JSON report path (default: reports/quantity_dirty_refresh_<ts>.json)",
    )
    args = parser.parse_args()

    create_tables()

    if args.modified_since:
        raw = args.modified_since.replace("Z", "+00:00")
        modified_since = datetime.fromisoformat(raw)
        if modified_since.tzinfo is None:
            modified_since = modified_since.replace(tzinfo=timezone.utc)
    else:
        modified_since = datetime.now(timezone.utc) - timedelta(hours=args.since_hours)

    min_date = args.min_date
    max_date = args.max_date
    date.fromisoformat(min_date)
    date.fromisoformat(max_date)

    watch_keys = {
        (w["job_code"].upper(), w["cost_code"].upper()) for w in WATCHLIST
    }

    print(f"\n{BOLD}{'=' * 78}")
    print("QUANTITY DIRTY-DATE DISCOVERY + quantity_test REFRESH")
    print(f"{'=' * 78}{RESET}")
    print(f"  modifiedSince : {modified_since.isoformat()}")
    print(f"  Date window   : {min_date} -> {max_date}")
    print(f"  Dry-run       : {args.dry_run}")
    print(f"  Watchlist     : {len(WATCHLIST)} discrepant cost codes")
    print()

    client = HCSSClient()
    report: dict[str, Any] = {
        "run_at": datetime.now(timezone.utc).isoformat(),
        "modified_since": modified_since.isoformat(),
        "date_window": {"min": min_date, "max": max_date},
        "dry_run": args.dry_run,
        "watchlist": WATCHLIST,
    }

    # -- 0. DB coverage check ----------------------------------------------
    db = SessionLocal()
    try:
        present = get_quantity_test_days_present(db, min_date, max_date)
        present_set = set(present)
        requested = []
        cur = date.fromisoformat(min_date)
        last = date.fromisoformat(max_date)
        while cur <= last:
            requested.append(cur.isoformat())
            cur += timedelta(days=1)
        missing_days = [d for d in requested if d not in present_set]
        report["db_coverage"] = {
            "days_requested": len(requested),
            "days_present": len(present),
            "missing_days": missing_days,
        }
        print(f"{CYAN}[0] quantity_test coverage {min_date} -> {max_date}{RESET}")
        print(
            f"  Present: {len(present)} / {len(requested)} days"
        )
        if missing_days:
            print(
                f"  {YELLOW}MISSING days (sum will undercount vs HCSS): "
                f"{missing_days}{RESET}"
            )
        else:
            print(f"  {GREEN}All days present{RESET}")

        print(f"\n{CYAN}[1] Snapshot watchlist BEFORE refresh{RESET}")
        before_map = snapshot_watchlist(db, min_date, max_date)
        before_daily = daily_breakdown_for_codes(db, min_date, max_date, watch_keys)
        before_rows = []
        for w in WATCHLIST:
            key = (w["job_code"].upper(), w["cost_code"].upper())
            item = before_map.get(key)
            db_qty = float(item["installed_quantity"]) if item else None
            before_rows.append({
                "job_code": w["job_code"],
                "cost_code": w["cost_code"],
                "hcss_actual": w["hcss"],
                "user_reported_dashboard": w["dashboard"],
                "db_installed_before": db_qty,
                "gap_vs_hcss_before": (
                    round(w["hcss"] - db_qty, 4) if db_qty is not None else None
                ),
            })
            gap = before_rows[-1]["gap_vs_hcss_before"]
            print(
                f"  {w['job_code']:<12} {w['cost_code']:<14} "
                f"HCSS={w['hcss']:>10}  DB={db_qty if db_qty is not None else 'MISSING':>10}  "
                f"gap={gap if gap is not None else 'n/a'}"
            )
        report["watchlist_before"] = before_rows
        report["watchlist_daily_before"] = before_daily
    finally:
        db.close()

    # -- 2. Discover adjustments -------------------------------------------
    print(f"\n{CYAN}[2] GET /costTypeQuantities/adjustments?modifiedSince=...{RESET}")
    try:
        adj_rows = fetch_adjustments(client, modified_since)
    except Exception as exc:
        logger.exception("adjustments API failed: %s", exc)
        report["adjustments_error"] = str(exc)
        report["verdict"] = "FAIL - adjustments API error"
        _write_report(report, args.out)
        raise SystemExit(f"adjustments API failed: {exc}") from exc

    report["adjustments_raw_count"] = len(adj_rows)
    print(f"  Returned {len(adj_rows)} adjustment row(s)")

    if adj_rows:
        sample = adj_rows[0]
        report["adjustments_sample_fields"] = sorted(sample.keys())
        print(f"  Sample fields: {sorted(sample.keys())}")

    # Normalize + filter
    normalized: list[dict] = []
    dirty_dates: set[str] = set()
    dirty_by_job: dict[str, set[str]] = defaultdict(set)
    watchlist_hits: list[dict] = []

    for r in adj_rows:
        txn = _parse_day(r.get("date"))
        mod = r.get("lastModifiedPreciseDateTime") or r.get("lastModified") or ""
        job_code = (r.get("jobCode") or "").strip()
        cost_code = (r.get("costCode") or "").strip()
        entry = {
            "job_id": r.get("jobId") or "",
            "job_code": job_code,
            "cost_code_id": r.get("costCodeId") or "",
            "cost_code": cost_code,
            "transaction_date": txn,
            "quantity": _sf(r.get("quantity")),
            "revision": r.get("revision"),
            "unit": r.get("unitOfMeasure") or "",
            "note": r.get("note") or "",
            "last_modified": str(mod),
            "foreman_code": r.get("foremanCode") or "",
            "is_foreman_entity": r.get("isForemanEntity"),
            "in_window": bool(txn and min_date <= txn <= max_date),
        }
        normalized.append(entry)
        if entry["in_window"] and txn:
            dirty_dates.add(txn)
            dirty_by_job[job_code].add(txn)
        key = (job_code.upper(), cost_code.upper())
        if key in watch_keys:
            watchlist_hits.append(entry)

    # Sort for readable report
    normalized.sort(
        key=lambda x: (
            x.get("transaction_date") or "",
            x.get("job_code") or "",
            x.get("cost_code") or "",
        )
    )
    report["adjustments"] = normalized
    report["dirty_dates"] = sorted(dirty_dates)
    report["dirty_dates_count"] = len(dirty_dates)
    report["dirty_dates_by_job"] = {
        k: sorted(v) for k, v in sorted(dirty_by_job.items())
    }
    report["watchlist_hits_in_adjustments"] = watchlist_hits

    print(f"\n{CYAN}[3] Dirty transaction dates in window{RESET}")
    if not dirty_dates:
        print(
            f"  {YELLOW}No dirty dates in {min_date}->{max_date} "
            f"from this modifiedSince window.{RESET}"
        )
        print(
            "  If discrepancies remain, either:"
        )
        print("    ? edits are older than modifiedSince cutoff")
        print("    ? edits are NOT quantity-adjustments (progress/timecard only)")
        print("    ? missing calendar days in quantity_test (see [0])")
    else:
        print(f"  {GREEN}{len(dirty_dates)} dirty date(s):{RESET} {sorted(dirty_dates)}")
        print("  By job:")
        for jc, days in sorted(dirty_by_job.items()):
            print(f"    ? {jc or '?'}: {sorted(days)}")

    print(f"\n{CYAN}[4] Watchlist vs adjustments hits{RESET}")
    hit_keys = {
        (h["job_code"].upper(), h["cost_code"].upper()) for h in watchlist_hits
    }
    for w in WATCHLIST:
        key = (w["job_code"].upper(), w["cost_code"].upper())
        if key in hit_keys:
            hits = [
                h for h in watchlist_hits
                if h["job_code"].upper() == key[0]
                and h["cost_code"].upper() == key[1]
            ]
            dates = sorted({h["transaction_date"] for h in hits if h["transaction_date"]})
            print(
                f"  {GREEN}HIT{RESET}  {w['job_code']:<12} {w['cost_code']:<14} "
                f"-> txn dates {dates}  ({len(hits)} adj row(s))"
            )
        else:
            print(
                f"  {RED}MISS{RESET} {w['job_code']:<12} {w['cost_code']:<14} "
                f"- not in adjustments for this modifiedSince"
            )

    report["watchlist_hit_count"] = len(hit_keys)
    report["watchlist_miss_count"] = len(WATCHLIST) - len(hit_keys)

    # Print compact adjustment table (first 80)
    print(f"\n{CYAN}[5] Adjustment rows (up to 80){RESET}")
    print(
        f"  {'TxnDate':<12} {'Job':<12} {'CostCode':<14} {'Qty':>10} "
        f"{'Rev':>4}  LastModified"
    )
    print("  " + "-" * 90)
    for e in normalized[:80]:
        flag = " " if e["in_window"] else "*"
        print(
            f" {flag}{e['transaction_date'] or '?':<12} "
            f"{e['job_code']:<12} {e['cost_code']:<14} "
            f"{e['quantity']:>10.2f} {str(e['revision'] or ''):>4}  "
            f"{e['last_modified']}"
        )
    if len(normalized) > 80:
        print(f"  ... and {len(normalized) - 80} more (see JSON report)")
    if any(not e["in_window"] for e in normalized):
        print("  (* = transaction date outside min/max window - not refreshed)")

    # -- 6. Refresh dirty days ---------------------------------------------
    refreshed: list[dict] = []
    if not dirty_dates:
        print(f"\n{YELLOW}[6] Skip refresh - no dirty dates{RESET}")
        report["refresh"] = {"skipped": True, "reason": "no_dirty_dates"}
    elif args.dry_run:
        print(f"\n{YELLOW}[6] DRY-RUN - would refresh {sorted(dirty_dates)}{RESET}")
        report["refresh"] = {
            "skipped": True,
            "reason": "dry_run",
            "would_refresh": sorted(dirty_dates),
        }
    else:
        print(f"\n{CYAN}[6] Refresh quantity_test for {len(dirty_dates)} dirty day(s){RESET}")
        print("  Loading active jobs + cost-code metadata...")
        active = fetch_active_jobs(client)
        jobs = [
            j for j in active
            if j.get("id")
            and str(j.get("status") or "").lower() == "active"
            and not j.get("isDeleted", False)
        ]
        job_ids = [j["id"] for j in jobs]
        job_code_map = {
            j["id"]: (j.get("code") or j.get("jobCode") or "") for j in jobs
        }
        print(f"  Active jobs: {len(job_ids)}")
        cc_meta = fetch_cost_code_meta(client, job_ids, args.delay)
        print(f"  Cost codes: {len(cc_meta)}")

        db = SessionLocal()
        try:
            for i, day in enumerate(sorted(dirty_dates), 1):
                print(f"\n  [{i}/{len(dirty_dates)}] Refreshing {day}...")
                try:
                    installed = progress_for_day(client, job_ids, day, args.delay)
                    rows = build_day_rows(installed, cc_meta, job_code_map)
                    n = store_quantity_test(db, day, rows)
                    nonzero = sum(1 for r in rows if r["installed_quantity"] != 0)
                    print(f"    Stored {n} rows ({nonzero} with installed != 0)")
                    refreshed.append({
                        "date": day,
                        "rows": n,
                        "nonzero": nonzero,
                        "ok": True,
                    })
                except Exception as exc:
                    logger.exception("Failed refresh %s: %s", day, exc)
                    print(f"    {RED}FAIL: {exc}{RESET}")
                    refreshed.append({"date": day, "ok": False, "error": str(exc)})
                    try:
                        db.rollback()
                    except Exception:
                        pass
        finally:
            db.close()
        report["refresh"] = {
            "skipped": False,
            "days": refreshed,
            "ok_count": sum(1 for r in refreshed if r.get("ok")),
            "fail_count": sum(1 for r in refreshed if not r.get("ok")),
        }

    # -- 7. Snapshot AFTER -------------------------------------------------
    print(f"\n{CYAN}[7] Snapshot watchlist AFTER refresh{RESET}")
    db = SessionLocal()
    try:
        after_map = snapshot_watchlist(db, min_date, max_date)
        after_daily = daily_breakdown_for_codes(db, min_date, max_date, watch_keys)
        after_rows = []
        improved = unchanged = worsened = missing = 0
        for w in WATCHLIST:
            key = (w["job_code"].upper(), w["cost_code"].upper())
            item = after_map.get(key)
            db_qty = float(item["installed_quantity"]) if item else None
            before = next(
                (
                    b["db_installed_before"]
                    for b in report["watchlist_before"]
                    if b["job_code"].upper() == key[0]
                    and b["cost_code"].upper() == key[1]
                ),
                None,
            )
            gap_after = (
                round(w["hcss"] - db_qty, 4) if db_qty is not None else None
            )
            gap_before = (
                round(w["hcss"] - before, 4) if before is not None else None
            )
            delta_db = (
                round(db_qty - before, 4)
                if db_qty is not None and before is not None
                else None
            )
            status = "MISSING"
            if gap_after is None:
                missing += 1
            elif abs(gap_after) <= 0.01:
                status = "MATCH"
                improved += 1
            elif gap_before is not None and abs(gap_after) < abs(gap_before) - 0.001:
                status = "IMPROVED"
                improved += 1
            elif gap_before is not None and abs(gap_after) > abs(gap_before) + 0.001:
                status = "WORSENED"
                worsened += 1
            else:
                status = "UNCHANGED"
                unchanged += 1

            color = {
                "MATCH": GREEN,
                "IMPROVED": GREEN,
                "UNCHANGED": YELLOW,
                "WORSENED": RED,
                "MISSING": RED,
            }.get(status, RESET)
            print(
                f"  {color}{status:<10}{RESET} {w['job_code']:<12} {w['cost_code']:<14} "
                f"before={before if before is not None else 'n/a':>10}  "
                f"after={db_qty if db_qty is not None else 'n/a':>10}  "
                f"ddb={delta_db if delta_db is not None else 'n/a':>8}  "
                f"gap_after={gap_after if gap_after is not None else 'n/a'}"
            )
            after_rows.append({
                "job_code": w["job_code"],
                "cost_code": w["cost_code"],
                "hcss_actual": w["hcss"],
                "db_installed_before": before,
                "db_installed_after": db_qty,
                "db_delta": delta_db,
                "gap_vs_hcss_before": gap_before,
                "gap_vs_hcss_after": gap_after,
                "status": status,
            })
        report["watchlist_after"] = after_rows
        report["watchlist_daily_after"] = after_daily
        report["watchlist_summary"] = {
            "match_or_improved": improved,
            "unchanged": unchanged,
            "worsened": worsened,
            "missing": missing,
        }
    finally:
        db.close()

    # -- Verdict -----------------------------------------------------------
    print(f"\n{BOLD}{'=' * 78}")
    print("VERDICT")
    print(f"{'=' * 78}{RESET}")

    if report.get("db_coverage", {}).get("missing_days"):
        print(
            f"  {YELLOW}NOTE: quantity_test is missing days "
            f"{report['db_coverage']['missing_days']} - "
            f"range sums will undercount even if dirty refresh works.{RESET}"
        )

    if not adj_rows:
        verdict = (
            "INCONCLUSIVE - adjustments API returned 0 rows for this "
            "modifiedSince window. Widen --since-hours / --modified-since, "
            "or edits may not be quantity-adjustments."
        )
        color = YELLOW
    elif not dirty_dates:
        verdict = (
            "INCONCLUSIVE - adjustments returned rows but none had "
            "transaction dates in the comparison window."
        )
        color = YELLOW
    elif args.dry_run:
        verdict = (
            f"DRY-RUN complete - {len(dirty_dates)} dirty date(s) found; "
            "re-run without --dry-run to refresh."
        )
        color = YELLOW
    else:
        summ = report.get("watchlist_summary") or {}
        if summ.get("unchanged", 0) == len(WATCHLIST) and summ.get("match_or_improved", 0) == 0:
            verdict = (
                "LIKELY FAIL for these discrepancies - dirty refresh ran but "
                "watchlist DB values did not move toward HCSS. "
                "adjustments?modifiedSince may not cover these edit types, "
                "or dirty dates did not include the days that hold the missing qty."
            )
            color = RED
        elif summ.get("match_or_improved", 0) > 0 and summ.get("unchanged", 0) == 0 and summ.get("worsened", 0) == 0:
            # check if all matched
            all_match = all(
                r.get("status") == "MATCH" for r in report.get("watchlist_after") or []
            )
            if all_match:
                verdict = (
                    "PASS - dirty-date refresh moved all watchlist rows to match HCSS."
                )
                color = GREEN
            else:
                verdict = (
                    "PARTIAL PASS - some watchlist rows improved/matched; "
                    "re-compare full range vs HCSS to confirm."
                )
                color = GREEN
        elif summ.get("match_or_improved", 0) > 0:
            verdict = (
                "PARTIAL - some discrepancies improved via dirty refresh; "
                "others unchanged. Method helps but may be incomplete."
            )
            color = YELLOW
        else:
            verdict = (
                "FAIL - refresh completed but watchlist gaps vs HCSS unchanged/worsened."
            )
            color = RED

    report["verdict"] = verdict
    print(f"  {color}{verdict}{RESET}")
    print()
    print("  Next step for you:")
    print(
        f"    GET /api/quantity-test/verify?start_date={min_date}&end_date={max_date}"
    )
    print("    Compare again to HCSS Cost Code Summary for the same range.")
    print()

    out_path = _write_report(report, args.out)
    print(f"  Full JSON report: {out_path}")
    print(f"{'=' * 78}\n")


def _write_report(report: dict, out_arg: str) -> Path:
    if out_arg:
        path = Path(out_arg)
    else:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = _REPO_ROOT / "reports" / f"quantity_dirty_refresh_{ts}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    return path


if __name__ == "__main__":
    main()
