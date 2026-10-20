"""
test_quantity_jobcosts_dirty.py
================================================================================
Test whether jobCosts/advancedRequest?modifiedSince can discover quantity dirty
dates (the channel budget already uses) — since costTypeQuantities/adjustments
returned 0 rows for this tenant.

Steps:
  1. POST /v1/jobCosts/advancedRequest {modifiedSince}  -> collect transaction dates
  2. Report: raw fields, dirty dates in window, watchlist hits, do the known
     stale days (2026-07-15 / 17 / 20) appear?
  3. Refresh quantity_test for those dirty dates (progress per day, overwrite)
  4. Compare watchlist vs HCSS actual, write JSON report

Does NOT touch production quantity_tracking_* tables or cron/routes.

USAGE:
  python scripts/test_quantity_jobcosts_dirty.py --since-hours 96
  python scripts/test_quantity_jobcosts_dirty.py --modified-since 2026-07-14T00:00:00Z
  python scripts/test_quantity_jobcosts_dirty.py --dry-run
  python scripts/test_quantity_jobcosts_dirty.py --modified-since 2026-05-01T00:00:00Z --min-date 2026-05-01 --max-date 2026-07-20
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import logging
import sys
import time
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

_REPO = Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from dotenv import load_dotenv

load_dotenv(_REPO / ".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)-8s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("quantity_jobcosts_dirty")

from payroll_verification.hcss_client import HCSSClient
from api.database import SessionLocal, create_tables, QuantityTest
from api.db_services import store_quantity_test, sum_quantity_test_for_period

_pop_spec = importlib.util.spec_from_file_location(
    "populate_quantity_test", _REPO / "scripts" / "populate_quantity_test.py"
)
_pop = importlib.util.module_from_spec(_pop_spec)
assert _pop_spec.loader is not None
_pop_spec.loader.exec_module(_pop)
build_day_rows = _pop.build_day_rows
fetch_active_jobs = _pop.fetch_active_jobs
fetch_cost_code_meta = _pop.fetch_cost_code_meta
progress_for_day = _pop.progress_for_day

_JOB_COSTS_V1 = "https://api.hcssapps.com/heavyjob/api/v1/jobCosts/advancedRequest"

# Known stale days found by the earlier day-scan experiment
KNOWN_STALE = {"2026-07-15", "2026-07-17", "2026-07-20"}

WATCHLIST = [
    {"job_code": "25006", "cost_code": "80711111", "hcss": 26557.0},
    {"job_code": "25006", "cost_code": "80011111B", "hcss": 11326.0},
    {"job_code": "25006", "cost_code": "58011111D", "hcss": 600.0},
    {"job_code": "25069C", "cost_code": "14511111C", "hcss": 565.9},
    {"job_code": "26004", "cost_code": "61340010A", "hcss": 19.5},
    {"job_code": "25060CONST", "cost_code": "40011111", "hcss": 51.0},
    {"job_code": "25060CONST", "cost_code": "24011111A", "hcss": 185.97},
    {"job_code": "25048", "cost_code": "12011111C", "hcss": 5.06},
    {"job_code": "25006", "cost_code": "58511111C", "hcss": 7.95},
    {"job_code": "25006", "cost_code": "5111111", "hcss": 9.24},
    {"job_code": "25060CONST", "cost_code": "211111I", "hcss": 7.71},
    {"job_code": "25006", "cost_code": "60011111B", "hcss": 0.05},
    {"job_code": "25006", "cost_code": "1011111", "hcss": 2.38},
]
WATCH_JOB_CODES = sorted({w["job_code"].upper() for w in WATCHLIST})
WATCH_KEYS = {(w["job_code"].upper(), w["cost_code"].upper()) for w in WATCHLIST}
TOL = 0.01


def _sf(v: Any) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def _day(v: Any) -> str | None:
    if not v:
        return None
    s = str(v).strip()
    return s[:10] if len(s) >= 10 else None


def _paginate_post(client: HCSSClient, url: str, payload: dict) -> list[dict]:
    rows: list[dict] = []
    cursor = None
    page = 0
    while True:
        body = dict(payload)
        if cursor:
            body["cursor"] = cursor
        raw = client._request("POST", url, json=body)
        batch, nxt = client._extract_results_and_cursor(raw)
        page += 1
        rows.extend(batch)
        logger.info("jobCosts page %d: %d rows (total %d)", page, len(batch), len(rows))
        if not nxt:
            break
        cursor = nxt
    return rows


def _extract_job_cost_fields(row: dict) -> dict:
    """v1 is flat; v2 nests job/costCode. Handle both."""
    job = row.get("job") or {}
    cc = row.get("costCode") or {}
    job_id = row.get("jobId") or job.get("jobId") or ""
    job_code = row.get("jobCode") or job.get("jobCode") or ""
    ccid = row.get("costCodeId") or cc.get("costCodeId") or ""
    ccode = (
        row.get("costCode")
        if isinstance(row.get("costCode"), str)
        else (row.get("costCodeCode") or cc.get("costCodeCode") or "")
    )
    return {
        "job_id": job_id,
        "job_code": job_code,
        "cost_code_id": ccid,
        "cost_code": ccode,
        "date": _day(row.get("date")),
        "quantity": _sf(row.get("quantity")),
        "entry_type": row.get("entryType") or "",
        "labor_cost": _sf(row.get("laborCost")),
        "equipment_cost": _sf(row.get("equipmentCost")),
    }


def _write(report: dict, out: str) -> Path:
    if out:
        p = Path(out)
    else:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        p = _REPO / "reports" / f"quantity_jobcosts_dirty_{ts}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    return p


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--modified-since", default="")
    ap.add_argument("--since-hours", type=float, default=96.0)
    ap.add_argument("--min-date", default="2026-05-01")
    ap.add_argument("--max-date", default="2026-07-20")
    ap.add_argument("--delay", type=float, default=0.3)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    create_tables()

    if args.modified_since:
        raw = args.modified_since.replace("Z", "+00:00")
        ms = datetime.fromisoformat(raw)
        if ms.tzinfo is None:
            ms = ms.replace(tzinfo=timezone.utc)
    else:
        ms = datetime.now(timezone.utc) - timedelta(hours=args.since_hours)

    min_d, max_d = args.min_date, args.max_date
    date.fromisoformat(min_d)
    date.fromisoformat(max_d)
    ms_str = ms.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")

    print("=" * 80)
    print("QUANTITY DIRTY DISCOVERY via jobCosts/advancedRequest?modifiedSince")
    print("=" * 80)
    print(f"  modifiedSince : {ms_str}")
    print(f"  window        : {min_d} -> {max_d}")
    print(f"  dry-run       : {args.dry_run}")
    print(f"  known stale   : {sorted(KNOWN_STALE)}")

    client = HCSSClient()
    report: dict[str, Any] = {
        "run_at": datetime.now(timezone.utc).isoformat(),
        "modified_since": ms_str,
        "window": {"min": min_d, "max": max_d},
        "dry_run": args.dry_run,
        "known_stale_days": sorted(KNOWN_STALE),
    }

    # ---- 1. jobCosts modifiedSince -------------------------------------------
    print("\n[1] POST jobCosts/advancedRequest {modifiedSince}...")
    try:
        rows = _paginate_post(
            client, _JOB_COSTS_V1, {"modifiedSince": ms_str, "limit": 500}
        )
    except Exception as exc:
        logger.exception("jobCosts failed")
        report["error"] = str(exc)
        report["verdict"] = "FAIL - jobCosts request error"
        p = _write(report, args.out)
        raise SystemExit(f"jobCosts failed: {exc} (report {p})")

    print(f"  returned {len(rows)} row(s)")
    report["jobcosts_raw_count"] = len(rows)
    if rows:
        report["jobcosts_sample_fields"] = sorted(rows[0].keys())
        print(f"  sample fields: {sorted(rows[0].keys())}")

    parsed = [_extract_job_cost_fields(r) for r in rows]

    dirty_dates: set[str] = set()
    dirty_all: set[str] = set()
    by_job: dict[str, set[str]] = defaultdict(set)
    entry_types = Counter()
    watch_hits: list[dict] = []

    for e in parsed:
        d = e["date"]
        entry_types[e["entry_type"]] += 1
        if d:
            dirty_all.add(d)
            if min_d <= d <= max_d:
                dirty_dates.add(d)
                by_job[e["job_code"]].add(d)
        key = ((e["job_code"] or "").upper(), (e["cost_code"] or "").upper())
        if key in WATCH_KEYS:
            watch_hits.append(e)

    report["entry_types"] = dict(entry_types)
    report["dirty_dates_in_window"] = sorted(dirty_dates)
    report["dirty_dates_all"] = sorted(dirty_all)
    report["dirty_by_job"] = {k: sorted(v) for k, v in sorted(by_job.items())}
    report["watchlist_hits"] = watch_hits

    print(f"\n[2] Dirty dates in window: {len(dirty_dates)}")
    print(f"  {sorted(dirty_dates)}")
    print(f"  entry types seen: {dict(entry_types)}")

    # Does discovery surface the known stale days?
    covered = KNOWN_STALE & dirty_dates
    missed = KNOWN_STALE - dirty_dates
    report["known_stale_covered"] = sorted(covered)
    report["known_stale_missed"] = sorted(missed)
    print(f"\n[3] Known stale days coverage:")
    print(f"  covered: {sorted(covered)}")
    print(f"  missed : {sorted(missed)}")

    print(f"\n[4] Watchlist job/cost-code hits in jobCosts: {len(watch_hits)}")
    wk_hit = {((h['job_code'] or '').upper(), (h['cost_code'] or '').upper()) for h in watch_hits}
    for w in WATCHLIST:
        key = (w["job_code"].upper(), w["cost_code"].upper())
        mark = "HIT " if key in wk_hit else "miss"
        print(f"  {mark} {w['job_code']:<12} {w['cost_code']:<14}")

    # ---- Snapshot before ------------------------------------------------------
    db = SessionLocal()
    try:
        before = sum_quantity_test_for_period(db, min_d, max_d)
    finally:
        db.close()
    before_idx = {
        ((i.get("job_code") or "").upper(), (i.get("cost_code") or "").upper()): i
        for i in before
    }
    report["watchlist_before"] = []
    for w in WATCHLIST:
        key = (w["job_code"].upper(), w["cost_code"].upper())
        it = before_idx.get(key)
        q = float(it["installed_quantity"]) if it else None
        report["watchlist_before"].append({
            **w,
            "db_before": q,
            "gap_before": round(w["hcss"] - q, 4) if q is not None else None,
        })

    if not dirty_dates:
        print("\nNo dirty dates -> nothing to refresh.")
        report["verdict"] = (
            "INCONCLUSIVE - jobCosts modifiedSince returned no in-window dirty dates. "
            "Widen --since-hours / --modified-since."
        )
        p = _write(report, args.out)
        print(f"Report: {p}")
        return

    if args.dry_run:
        print(f"\n[5] DRY-RUN - would refresh {sorted(dirty_dates)}")
        report["verdict"] = (
            f"DRY-RUN - {len(dirty_dates)} dirty date(s); "
            f"known stale covered={sorted(covered)} missed={sorted(missed)}"
        )
        p = _write(report, args.out)
        print(f"Report: {p}")
        return

    # ---- 5. Refresh dirty dates in quantity_test ------------------------------
    print(f"\n[5] Refresh quantity_test for {len(dirty_dates)} dirty day(s)...")
    active = fetch_active_jobs(client)
    all_ids = [j["id"] for j in active if j.get("id")]
    code_map = {
        j["id"]: (j.get("code") or j.get("jobCode") or "") for j in active if j.get("id")
    }
    print(f"  active jobs: {len(all_ids)}; fetching cost-code meta...")
    meta = fetch_cost_code_meta(client, all_ids, args.delay)
    print(f"  cost codes: {len(meta)}")

    refreshed = []
    db = SessionLocal()
    try:
        for i, d in enumerate(sorted(dirty_dates), 1):
            print(f"  [{i}/{len(dirty_dates)}] {d} ...")
            try:
                installed = progress_for_day(client, all_ids, d, args.delay)
                day_rows = build_day_rows(installed, meta, code_map)
                n = store_quantity_test(db, d, day_rows)
                print(f"    stored {n} rows")
                refreshed.append({"date": d, "rows": n, "ok": True})
            except Exception as exc:
                logger.exception("refresh failed %s", d)
                refreshed.append({"date": d, "ok": False, "error": str(exc)})
                try:
                    db.rollback()
                except Exception:
                    pass
    finally:
        db.close()
    report["refresh"] = refreshed

    # ---- 6. Compare after -----------------------------------------------------
    db = SessionLocal()
    try:
        after = sum_quantity_test_for_period(db, min_d, max_d)
    finally:
        db.close()
    after_idx = {
        ((i.get("job_code") or "").upper(), (i.get("cost_code") or "").upper()): i
        for i in after
    }

    print("\n[6] Watchlist after refresh:")
    match = improved = unchanged = worsened = 0
    rows_out = []
    for w in WATCHLIST:
        key = (w["job_code"].upper(), w["cost_code"].upper())
        before_q = next(
            (b["db_before"] for b in report["watchlist_before"]
             if (b["job_code"].upper(), b["cost_code"].upper()) == key),
            None,
        )
        it = after_idx.get(key)
        after_q = float(it["installed_quantity"]) if it else None
        gap_b = round(w["hcss"] - before_q, 4) if before_q is not None else None
        gap_a = round(w["hcss"] - after_q, 4) if after_q is not None else None
        if gap_a is not None and abs(gap_a) <= TOL:
            status = "MATCH"; match += 1
        elif gap_b is not None and gap_a is not None and abs(gap_a) < abs(gap_b) - TOL:
            status = "IMPROVED"; improved += 1
        elif gap_b is not None and gap_a is not None and abs(gap_a) > abs(gap_b) + TOL:
            status = "WORSENED"; worsened += 1
        else:
            status = "UNCHANGED"; unchanged += 1
        print(
            f"  {status:<10} {w['job_code']:<12} {w['cost_code']:<14} "
            f"before={before_q} after={after_q} hcss={w['hcss']} gap_after={gap_a}"
        )
        rows_out.append({
            "job_code": w["job_code"], "cost_code": w["cost_code"], "hcss": w["hcss"],
            "db_before": before_q, "db_after": after_q,
            "gap_before": gap_b, "gap_after": gap_a, "status": status,
        })
    report["watchlist_after"] = rows_out
    report["summary"] = {
        "match": match, "improved": improved,
        "unchanged": unchanged, "worsened": worsened,
        "dirty_dates": len(dirty_dates),
        "known_stale_covered": sorted(covered),
        "known_stale_missed": sorted(missed),
    }

    # ---- Verdict --------------------------------------------------------------
    all_match = match == len(WATCHLIST)
    if all_match and not missed:
        verdict = (
            "PASS - jobCosts?modifiedSince surfaced all known stale days AND "
            "refresh matched every watchlist row to HCSS. Viable dirty-date channel."
        )
    elif all_match and missed:
        verdict = (
            "PASS (values) but jobCosts missed some known stale days "
            f"{sorted(missed)} - they were still refreshed via other dirty rows or lookback. "
            "Review coverage before relying solely on jobCosts."
        )
    elif match + improved > 0:
        verdict = (
            "PARTIAL - jobCosts discovery helped; some rows still off. "
            f"covered={sorted(covered)} missed={sorted(missed)}"
        )
    else:
        verdict = (
            "FAIL - jobCosts discovery did not fix watchlist gaps. "
            f"covered={sorted(covered)} missed={sorted(missed)}"
        )
    report["verdict"] = verdict
    print("\n" + "=" * 80)
    print("VERDICT")
    print("=" * 80)
    print(verdict)
    p = _write(report, args.out)
    print(f"\nReport: {p}")


if __name__ == "__main__":
    main()
