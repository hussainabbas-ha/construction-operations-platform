"""
Find calendar days where quantity_test disagrees with live HCSS progress
for the user's discrepant watchlist jobs, then refresh those days.

Used when adjustments?modifiedSince returns nothing (this tenant).
Does NOT touch production quantity_tracking_* tables.
"""
from __future__ import annotations

import importlib.util
import json
import sys
import time
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))

from dotenv import load_dotenv

load_dotenv(_REPO / ".env")

from payroll_verification.hcss_client import HCSSClient
from api.database import SessionLocal, create_tables, QuantityTest
from api.db_services import store_quantity_test, sum_quantity_test_for_period

_pop_spec = importlib.util.spec_from_file_location(
    "populate_quantity_test",
    _REPO / "scripts" / "populate_quantity_test.py",
)
_pop = importlib.util.module_from_spec(_pop_spec)
assert _pop_spec.loader is not None
_pop_spec.loader.exec_module(_pop)

build_day_rows = _pop.build_day_rows
fetch_active_jobs = _pop.fetch_active_jobs
fetch_cost_code_meta = _pop.fetch_cost_code_meta
progress_for_day = _pop.progress_for_day
_paginate_get = _pop._paginate_get
_paginate_post = _pop._paginate_post
_sf = _pop._sf

_PROGRESS = "https://api.hcssapps.com/heavyjob/api/v1/costCode/progress/advancedRequest"
_JOBS_ADV = "https://api.hcssapps.com/heavyjob/api/v1/jobs/advanced"

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
WATCH_KEYS = {
    (w["job_code"].upper(), w["cost_code"].upper()) for w in WATCHLIST
}
TOL = 0.01


def date_list(start: str, end: str) -> list[str]:
    cur = date.fromisoformat(start)
    last = date.fromisoformat(end)
    out = []
    while cur <= last:
        out.append(cur.isoformat())
        cur += timedelta(days=1)
    return out


def resolve_jobs(client: HCSSClient) -> dict[str, str]:
    """job_code upper -> job_id for watchlist jobs via active jobs list."""
    active = fetch_active_jobs(client)
    out: dict[str, str] = {}
    for j in active:
        code = (j.get("code") or j.get("jobCode") or "").strip().upper()
        jid = j.get("id") or ""
        if code in WATCH_JOB_CODES and jid:
            out[code] = jid
    missing = [c for c in WATCH_JOB_CODES if c not in out]
    if missing:
        print(f"  WARNING: not found in active jobs: {missing}")
    return out


def db_daily_for_watch(db, start: str, end: str) -> dict[tuple[str, str, str], float]:
    """(job_code, cost_code, date) -> installed."""
    rows = (
        db.query(QuantityTest)
        .filter(
            QuantityTest.tracking_date >= start,
            QuantityTest.tracking_date <= end,
        )
        .all()
    )
    out: dict[tuple[str, str, str], float] = {}
    for r in rows:
        key = ((r.job_code or "").upper(), (r.cost_code or "").upper())
        if key not in WATCH_KEYS:
            continue
        out[(key[0], key[1], r.tracking_date)] = float(r.installed_quantity or 0)
    return out


def hcss_day_qty(
    client: HCSSClient, job_id: str, day: str, delay: float
) -> dict[str, float]:
    """cost_code_id -> qty for one job one day. Also return by... we need cost codes.
    Return costCodeId -> quantity; caller maps via meta.
    """
    rows = _paginate_post(
        client,
        _PROGRESS,
        {
            "jobId": job_id,
            "startDate": f"{day}T00:00:00Z",
            "endDate": f"{day}T23:59:59Z",
            "limit": 500,
        },
    )
    totals: dict[str, float] = defaultdict(float)
    for p in rows:
        ccid = p.get("costCodeId") or p.get("id") or ""
        if ccid:
            totals[ccid] += _sf(p.get("quantity"))
    time.sleep(delay)
    return dict(totals)


def main() -> None:
    start, end = "2026-05-01", "2026-07-20"
    delay = 0.25
    create_tables()
    client = HCSSClient()
    days = date_list(start, end)

    print("=" * 78)
    print("FIND STALE DAYS (HCSS progress vs quantity_test) THEN REFRESH")
    print("=" * 78)
    print(f"  Range: {start} -> {end} ({len(days)} days)")
    print(f"  Watch jobs: {WATCH_JOB_CODES}")

    print("\n[1] Resolve watchlist job IDs...")
    job_map = resolve_jobs(client)  # code -> id
    print(f"  Resolved: {job_map}")
    if not job_map:
        raise SystemExit("No watchlist jobs resolved")

    print("\n[2] Load cost-code meta for watch jobs...")
    job_ids = list(job_map.values())
    cc_meta = fetch_cost_code_meta(client, job_ids, delay)
    # Map (job_code, cost_code) -> cost_code_id
    code_to_id: dict[tuple[str, str], str] = {}
    id_to_key: dict[str, tuple[str, str]] = {}
    job_id_to_code = {v: k for k, v in job_map.items()}
    for ccid, meta in cc_meta.items():
        jid = meta["job_id"]
        jcode = job_id_to_code.get(jid, "")
        ccode = (meta["cost_code"] or "").upper()
        if not jcode:
            # try from job_map reverse via meta job
            continue
        key = (jcode, ccode)
        if key in WATCH_KEYS:
            code_to_id[key] = ccid
            id_to_key[ccid] = key
    print(f"  Mapped {len(code_to_id)}/{len(WATCH_KEYS)} watchlist cost codes to IDs")
    for k in sorted(WATCH_KEYS):
        print(f"    {k[0]} {k[1]} -> {code_to_id.get(k, 'NOT FOUND')}")

    print("\n[3] Load DB daily watchlist quantities...")
    db = SessionLocal()
    try:
        db_map = db_daily_for_watch(db, start, end)
        before = sum_quantity_test_for_period(db, start, end)
    finally:
        db.close()
    before_idx = {
        ((i.get("job_code") or "").upper(), (i.get("cost_code") or "").upper()): i
        for i in before
    }

    print("\n[4] Scan HCSS progress day-by-day for watch jobs (find stale days)...")
    day_deltas: list[dict] = []
    dirty_dates: set[str] = set()

    for i, day in enumerate(days, 1):
        if i % 10 == 1 or i == len(days):
            print(f"  scanning {day} [{i}/{len(days)}] dirty_so_far={len(dirty_dates)}")
        # Fetch each watch job once for this day
        live_by_ccid: dict[str, float] = {}
        for jcode, jid in job_map.items():
            part = hcss_day_qty(client, jid, day, delay)
            for ccid, qty in part.items():
                if ccid in id_to_key:
                    live_by_ccid[ccid] = live_by_ccid.get(ccid, 0.0) + qty

        for key, ccid in code_to_id.items():
            live = round(live_by_ccid.get(ccid, 0.0), 4)
            db_qty = round(db_map.get((key[0], key[1], day), 0.0), 4)
            delta = round(live - db_qty, 4)
            if abs(delta) > TOL:
                dirty_dates.add(day)
                day_deltas.append({
                    "date": day,
                    "job_code": key[0],
                    "cost_code": key[1],
                    "hcss_day": live,
                    "db_day": db_qty,
                    "delta": delta,
                })

    print(f"\n  Found {len(dirty_dates)} dirty date(s), {len(day_deltas)} cell delta(s)")
    print(f"  Dirty dates: {sorted(dirty_dates)}")
    print("\n  Top deltas:")
    for d in sorted(day_deltas, key=lambda x: -abs(x["delta"]))[:40]:
        print(
            f"    {d['date']} {d['job_code']:<12} {d['cost_code']:<14} "
            f"HCSS={d['hcss_day']:>10} DB={d['db_day']:>10} d={d['delta']:>10}"
        )

    report: dict[str, Any] = {
        "run_at": datetime.now(timezone.utc).isoformat(),
        "method": "day_by_day_progress_diff_vs_quantity_test",
        "range": {"start": start, "end": end},
        "adjustments_api_note": (
            "Prior probe returned 0 rows for modifiedSince and startDate/endDate "
            "across May1-Jul20 — adjustments dirty discovery not usable for this tenant."
        ),
        "dirty_dates": sorted(dirty_dates),
        "day_deltas": day_deltas,
        "watchlist_before": [],
    }
    for w in WATCHLIST:
        key = (w["job_code"].upper(), w["cost_code"].upper())
        item = before_idx.get(key)
        db_qty = float(item["installed_quantity"]) if item else None
        report["watchlist_before"].append({
            **w,
            "db_before": db_qty,
            "gap_before": round(w["hcss"] - db_qty, 4) if db_qty is not None else None,
        })

    if not dirty_dates:
        print("\nNo day-level diffs found — unexpected if period totals still differ.")
        print("Possible causes: cost code ID mapping miss, or HCSS period uses different source.")
        report["verdict"] = "NO_DAY_DIFFS_FOUND"
        out = _REPO / "reports" / f"quantity_stale_day_refresh_{datetime.now():%Y%m%d_%H%M%S}.json"
        out.parent.mkdir(exist_ok=True)
        out.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
        print(f"Report: {out}")
        return

    print(f"\n[5] Refresh ALL active jobs for {len(dirty_dates)} dirty day(s) into quantity_test...")
    active = fetch_active_jobs(client)
    all_job_ids = [j["id"] for j in active if j.get("id")]
    job_code_map = {
        j["id"]: (j.get("code") or j.get("jobCode") or "") for j in active if j.get("id")
    }
    print(f"  Active jobs for full-day rewrite: {len(all_job_ids)}")
    print("  Fetching full cost-code meta...")
    full_meta = fetch_cost_code_meta(client, all_job_ids, delay)

    refreshed = []
    db = SessionLocal()
    try:
        for i, day in enumerate(sorted(dirty_dates), 1):
            print(f"  [{i}/{len(dirty_dates)}] Refreshing {day}...")
            try:
                installed = progress_for_day(client, all_job_ids, day, delay)
                rows = build_day_rows(installed, full_meta, job_code_map)
                n = store_quantity_test(db, day, rows)
                print(f"    stored {n} rows")
                refreshed.append({"date": day, "rows": n, "ok": True})
            except Exception as exc:
                print(f"    FAIL {exc}")
                refreshed.append({"date": day, "ok": False, "error": str(exc)})
                try:
                    db.rollback()
                except Exception:
                    pass
    finally:
        db.close()
    report["refresh"] = refreshed

    print("\n[6] Watchlist AFTER refresh...")
    db = SessionLocal()
    try:
        after = sum_quantity_test_for_period(db, start, end)
    finally:
        db.close()
    after_idx = {
        ((i.get("job_code") or "").upper(), (i.get("cost_code") or "").upper()): i
        for i in after
    }
    after_rows = []
    match = improved = unchanged = worsened = 0
    for w in WATCHLIST:
        key = (w["job_code"].upper(), w["cost_code"].upper())
        before_q = next(
            (b["db_before"] for b in report["watchlist_before"] if (b["job_code"].upper(), b["cost_code"].upper()) == key),
            None,
        )
        item = after_idx.get(key)
        after_q = float(item["installed_quantity"]) if item else None
        gap_b = round(w["hcss"] - before_q, 4) if before_q is not None else None
        gap_a = round(w["hcss"] - after_q, 4) if after_q is not None else None
        if gap_a is not None and abs(gap_a) <= TOL:
            status = "MATCH"
            match += 1
        elif gap_b is not None and gap_a is not None and abs(gap_a) < abs(gap_b) - TOL:
            status = "IMPROVED"
            improved += 1
        elif gap_b is not None and gap_a is not None and abs(gap_a) > abs(gap_b) + TOL:
            status = "WORSENED"
            worsened += 1
        else:
            status = "UNCHANGED"
            unchanged += 1
        print(
            f"  {status:<10} {w['job_code']:<12} {w['cost_code']:<14} "
            f"before={before_q} after={after_q} gap_before={gap_b} gap_after={gap_a}"
        )
        after_rows.append({
            "job_code": w["job_code"],
            "cost_code": w["cost_code"],
            "hcss": w["hcss"],
            "db_before": before_q,
            "db_after": after_q,
            "gap_before": gap_b,
            "gap_after": gap_a,
            "status": status,
        })
    report["watchlist_after"] = after_rows
    report["summary"] = {
        "match": match,
        "improved": improved,
        "unchanged": unchanged,
        "worsened": worsened,
        "dirty_dates_count": len(dirty_dates),
    }

    if match == len(WATCHLIST):
        verdict = (
            "PASS (overwrite path) — refreshing stale days fixed all watchlist gaps. "
            "BUT adjustments?modifiedSince cannot discover those days (API returned 0). "
            "Dirty-date via adjustments FAILS for this tenant; need another discovery signal "
            "or broader lookback/reconcile."
        )
    elif match + improved > 0:
        verdict = (
            "PARTIAL — day refresh fixed/improved some gaps. adjustments discovery still "
            "unusable (0 rows). Overwrite works when dates are known."
        )
    else:
        verdict = (
            "FAIL — even after refreshing days that differed on day-scan, watchlist still "
            "gaps vs reported HCSS. Investigate period vs day-sum math or job coverage."
        )
    report["verdict"] = verdict
    print("\n" + "=" * 78)
    print("VERDICT")
    print("=" * 78)
    print(verdict)

    out = _REPO / "reports" / f"quantity_stale_day_refresh_{datetime.now():%Y%m%d_%H%M%S}.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(f"\nReport: {out}")


if __name__ == "__main__":
    main()
