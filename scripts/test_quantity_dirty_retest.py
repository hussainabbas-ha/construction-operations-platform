"""
Re-test dirty flow against user's NEW May1-Jul20 mismatches.

1. Snapshot watchlist from quantity_test (before)
2. Discover dirty dates via GET timeCardInfo?modifiedSince
3. Refresh those days into quantity_test
4. Snapshot after + report GO/NO-GO
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))

from dotenv import load_dotenv

load_dotenv(_REPO / ".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)-8s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("qty_dirty_retest")

from payroll_verification.hcss_client import HCSSClient
from api.database import SessionLocal, create_tables
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

_TIMECARDS = "https://api.hcssapps.com/heavyjob/api/v1/timeCardInfo"
MIN_D, MAX_D = "2026-05-01", "2026-07-20"
TOL = 0.01

# User's new discrepancies (HCSS Actual is truth)
WATCHLIST = [
    {"job_code": "25013", "cost_code": "12451111B", "hcss": 768.25, "dash_reported": 91.0},
    {"job_code": "25013", "cost_code": "12401111B", "hcss": 250.0, "dash_reported": 0.0},
    {"job_code": "25013", "cost_code": "71300000", "hcss": 293.0, "dash_reported": 78.0},
    {"job_code": "25013", "cost_code": "13351111E", "hcss": 100.0, "dash_reported": 0.0},
    {"job_code": "25060DIST", "cost_code": "3011111F", "hcss": 285.0, "dash_reported": 195.0},
    {"job_code": "25060DIST", "cost_code": "3011111C", "hcss": 359.0, "dash_reported": 269.0},
    {"job_code": "25013", "cost_code": "12451111F", "hcss": 75.0, "dash_reported": 0.0},
    {"job_code": "25013", "cost_code": "13351111A", "hcss": 761.18, "dash_reported": 691.88},
    {"job_code": "25006", "cost_code": "58011111D", "hcss": 660.0, "dash_reported": 600.0},
    {"job_code": "25060CONST", "cost_code": "49011111A", "hcss": 51.0, "dash_reported": 0.0},
    {"job_code": "25013", "cost_code": "12451111E", "hcss": 400.0, "dash_reported": 350.0},
    {"job_code": "25006", "cost_code": "58011111B", "hcss": 702.0, "dash_reported": 657.0},
    {"job_code": "25060DIST", "cost_code": "3011111B", "hcss": 344.0, "dash_reported": 307.0},
    {"job_code": "25013", "cost_code": "13351111D", "hcss": 13.0, "dash_reported": 0.0},
    {"job_code": "25060CONST", "cost_code": "24011111A", "hcss": 198.93, "dash_reported": 185.97},
    {"job_code": "25013", "cost_code": "13351111G", "hcss": 10.11, "dash_reported": 0.0},
    {"job_code": "25069C", "cost_code": "10511111", "hcss": 6.0, "dash_reported": 0.0},
    {"job_code": "25013", "cost_code": "12101111C", "hcss": 8.0, "dash_reported": 2.0},
    {"job_code": "25013", "cost_code": "12451111D", "hcss": 43.0, "dash_reported": 40.0},
    {"job_code": "25006", "cost_code": "19011111", "hcss": 22.2, "dash_reported": 21.9},
    {"job_code": "25006", "cost_code": "58511111B", "hcss": 5.51, "dash_reported": 5.31},
    {"job_code": "25013", "cost_code": "12001111A", "hcss": 0.07, "dash_reported": 0.05},
]
WATCH_KEYS = {(w["job_code"].upper(), w["cost_code"].upper()) for w in WATCHLIST}


def _day(v: Any) -> str | None:
    if not v:
        return None
    s = str(v).strip()
    return s[:10] if len(s) >= 10 else None


def snapshot(db) -> list[dict]:
    items = sum_quantity_test_for_period(db, MIN_D, MAX_D)
    idx = {
        ((i.get("job_code") or "").upper(), (i.get("cost_code") or "").upper()): i
        for i in items
    }
    rows = []
    for w in WATCHLIST:
        key = (w["job_code"].upper(), w["cost_code"].upper())
        it = idx.get(key)
        db_qty = float(it["installed_quantity"]) if it else None
        gap = round(w["hcss"] - db_qty, 4) if db_qty is not None else None
        status = (
            "MATCH"
            if gap is not None and abs(gap) <= TOL
            else ("MISSING" if db_qty is None else "GAP")
        )
        rows.append({
            **w,
            "db": db_qty,
            "gap_vs_hcss": gap,
            "status": status,
        })
    return rows


def discover_dirty(client: HCSSClient, ms: str, delay: float) -> dict:
    rows: list[dict] = []
    cursor = None
    page = 0
    while True:
        params = {"modifiedSince": ms, "limit": 1000}
        if cursor:
            params["cursor"] = cursor
        raw = client._request("GET", _TIMECARDS, params=params)
        batch, nxt = client._extract_results_and_cursor(raw)
        page += 1
        rows.extend(batch)
        logger.info("timeCardInfo page %d: +%d (total %d)", page, len(batch), len(rows))
        if not nxt:
            break
        cursor = nxt
        time.sleep(delay)

    dates_all = set()
    dates_window = set()
    samples = []
    for r in rows:
        d = _day(r.get("date"))
        if not d:
            continue
        dates_all.add(d)
        if MIN_D <= d <= MAX_D:
            dates_window.add(d)
        if len(samples) < 20:
            samples.append({
                "id": r.get("id"),
                "jobId": r.get("jobId"),
                "date": d,
                "revision": r.get("revision"),
                "lastModified": r.get("lastModifiedPreciseDateTime"),
            })
    return {
        "rows": len(rows),
        "pages": page,
        "dates_all": sorted(dates_all),
        "dates_in_window": sorted(dates_window),
        "sample": samples,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--modified-since",
        default="2026-07-21T14:00:00Z",
        help="Cursor: default = before last dirty-channel refresh (UTC)",
    )
    ap.add_argument("--delay", type=float, default=0.35)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    create_tables()
    ms = args.modified_since
    if ms.endswith("Z"):
        ms_api = ms.replace("Z", "")  # HCSS accepts both; keep Z in report
        ms_param = ms
    else:
        ms_param = ms

    print("=" * 78)
    print("QUANTITY DIRTY RETEST (new mismatch list)")
    print("=" * 78)
    print(f"  modifiedSince : {ms_param}")
    print(f"  range         : {MIN_D} -> {MAX_D}")
    print(f"  watchlist     : {len(WATCHLIST)} cost codes")
    print(f"  dry-run       : {args.dry_run}")

    report: dict[str, Any] = {
        "run_at": datetime.now(timezone.utc).isoformat(),
        "modified_since": ms_param,
        "range": {"start": MIN_D, "end": MAX_D},
        "watchlist": WATCHLIST,
    }

    db = SessionLocal()
    try:
        before = snapshot(db)
    finally:
        db.close()
    report["before"] = before
    gaps_before = sum(1 for r in before if r["status"] != "MATCH")
    print(f"\n[1] BEFORE — {gaps_before}/{len(before)} gaps vs HCSS")
    for r in before:
        print(
            f"  {r['status']:<7} {r['job_code']:<12} {r['cost_code']:<14} "
            f"db={r['db']} hcss={r['hcss']} gap={r['gap_vs_hcss']}"
        )

    client = HCSSClient()
    print(f"\n[2] Discover dirty dates via timeCardInfo?modifiedSince=...")
    disc = discover_dirty(client, ms_param, args.delay)
    report["discovery"] = disc
    dirty = disc["dates_in_window"]
    print(f"  rows={disc['rows']}  dirty dates in window={len(dirty)}")
    print(f"  dirty: {dirty}")

    if not dirty:
        report["verdict"] = (
            "INCONCLUSIVE — timeCardInfo returned 0 dirty dates in window for "
            f"modifiedSince={ms_param}. Widen the cursor (earlier timestamp)."
        )
        p = _write(report)
        print(f"\n{report['verdict']}\nReport: {p}")
        return

    if args.dry_run:
        report["verdict"] = f"DRY-RUN — would refresh {dirty}"
        p = _write(report)
        print(f"\n{report['verdict']}\nReport: {p}")
        return

    print(f"\n[3] Refresh {len(dirty)} dirty day(s)...")
    active = fetch_active_jobs(client)
    all_ids = [j["id"] for j in active if j.get("id")]
    code_map = {
        j["id"]: (j.get("code") or j.get("jobCode") or "") for j in active if j.get("id")
    }
    print(f"  active jobs={len(all_ids)}; fetching cost-code meta...")
    meta = fetch_cost_code_meta(client, all_ids, args.delay)
    print(f"  cost codes={len(meta)}")

    refreshed = []
    db = SessionLocal()
    try:
        for i, d in enumerate(dirty, 1):
            print(f"  [{i}/{len(dirty)}] {d}...")
            try:
                installed = progress_for_day(client, all_ids, d, args.delay)
                rows = build_day_rows(installed, meta, code_map)
                n = store_quantity_test(db, d, rows)
                print(f"    stored {n}")
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

    db = SessionLocal()
    try:
        after = snapshot(db)
    finally:
        db.close()
    report["after"] = after

    print(f"\n[4] AFTER refresh")
    match = improved = unchanged = worsened = 0
    for b, a in zip(before, after):
        gb, ga = b["gap_vs_hcss"], a["gap_vs_hcss"]
        if a["status"] == "MATCH":
            st = "MATCH"
            match += 1
        elif gb is not None and ga is not None and abs(ga) < abs(gb) - TOL:
            st = "IMPROVED"
            improved += 1
        elif gb is not None and ga is not None and abs(ga) > abs(gb) + TOL:
            st = "WORSENED"
            worsened += 1
        else:
            st = "UNCHANGED"
            unchanged += 1
        print(
            f"  {st:<10} {a['job_code']:<12} {a['cost_code']:<14} "
            f"before_gap={gb} after_gap={ga} db={a['db']}"
        )
        a["change"] = st

    report["summary"] = {
        "match": match,
        "improved": improved,
        "unchanged": unchanged,
        "worsened": worsened,
        "dirty_dates": dirty,
        "gaps_before": gaps_before,
        "gaps_after": sum(1 for r in after if r["status"] != "MATCH"),
    }

    if match == len(WATCHLIST):
        verdict = (
            "PASS — dirty discovery + refresh resolved ALL watchlist gaps. "
            "Safe to implement in main quantity logic."
        )
    elif match + improved > gaps_before * 0.5 and match > 0:
        verdict = (
            f"PARTIAL — match={match} improved={improved} unchanged={unchanged} "
            f"worsened={worsened}. Discovery helped but some gaps remain "
            "(edits may be older than modifiedSince, or non-timecard qty)."
        )
    elif match + improved == 0:
        verdict = (
            "FAIL — dirty dates refreshed but watchlist gaps did not move toward HCSS. "
            "Do NOT rely on this modifiedSince window alone."
        )
    else:
        verdict = (
            f"PARTIAL — match={match} improved={improved} unchanged={unchanged}. "
            "Investigate remaining gaps (date coverage / cursor)."
        )
    report["verdict"] = verdict
    print(f"\n{'='*78}\nVERDICT\n{'='*78}\n{verdict}")
    p = _write(report)
    print(f"\nReport: {p}")


def _write(report: dict) -> Path:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    p = _REPO / "reports" / f"quantity_dirty_retest_{ts}.json"
    p.parent.mkdir(exist_ok=True)
    p.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    return p


if __name__ == "__main__":
    main()
