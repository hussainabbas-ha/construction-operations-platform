"""
Thorough test: can we use HCSS modifiedSince endpoints for quantity dirty-date
discovery in production?

Channels tested:
  A. GET  /v1/timeCardInfo?modifiedSince=...
  B. POST /v1/costCodeTimeCardTransactions/advancedRequest {modifiedSince}
  C. POST /v2/jobCosts/advancedRequest {modifiedSince}   (v2, not the broken v1)

Known truth (from prior experiment):
  Stale days that caused May1-Jul20 drift: 2026-07-15, 2026-07-17, 2026-07-20
  Watchlist cost codes with known HCSS actuals (see WATCHLIST)

Pass criteria for "usable in main code":
  1. At least one channel returns a NON-firehose set (reasonable row count)
  2. That channel's dirty dates INCLUDE known stale days (or enough of them)
  3. Refreshing discovered dates restores watchlist to match HCSS

USAGE:
  python scripts/test_quantity_dirty_channels.py
  python scripts/test_quantity_dirty_channels.py --modified-since 2026-07-19T00:00:00Z
  python scripts/test_quantity_dirty_channels.py --skip-refresh
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
sys.path.insert(0, str(_REPO))

from dotenv import load_dotenv

load_dotenv(_REPO / ".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)-8s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("qty_dirty_channels")

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

_TIMECARDS = "https://api.hcssapps.com/heavyjob/api/v1/timeCardInfo"
_CC_TXN = (
    "https://api.hcssapps.com/heavyjob/api/v1/"
    "costCodeTimeCardTransactions/advancedRequest"
)
_JOB_COSTS_V2 = "https://api.hcssapps.com/heavyjob/api/v2/jobCosts/advancedRequest"

KNOWN_STALE = {"2026-07-15", "2026-07-17", "2026-07-20"}
MIN_D, MAX_D = "2026-05-01", "2026-07-20"
# If a channel returns more than this many DISTINCT txn dates in window,
# treat as firehose (unusable for precise dirty discovery).
FIREHOSE_DATE_LIMIT = 40
# Cap pagination so we don't burn the rate limit forever.
MAX_PAGES = 40

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
WATCH_KEYS = {(w["job_code"].upper(), w["cost_code"].upper()) for w in WATCHLIST}
TOL = 0.01


def _day(v: Any) -> str | None:
    if not v:
        return None
    s = str(v).strip()
    return s[:10] if len(s) >= 10 else None


def _sf(v: Any) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def paginate_get(
    client: HCSSClient, url: str, params: dict, *, max_pages: int, delay: float
) -> tuple[list[dict], bool, int]:
    rows: list[dict] = []
    cursor = None
    page = 0
    while True:
        p = dict(params)
        if cursor:
            p["cursor"] = cursor
        raw = client._request("GET", url, params=p)
        batch, nxt = client._extract_results_and_cursor(raw)
        page += 1
        rows.extend(batch)
        logger.info("%s page %d: +%d (total %d)", url.split("/")[-1], page, len(batch), len(rows))
        if not nxt:
            return rows, False, page
        if page >= max_pages:
            return rows, True, page
        cursor = nxt
        time.sleep(delay)


def paginate_post(
    client: HCSSClient, url: str, payload: dict, *, max_pages: int, delay: float
) -> tuple[list[dict], bool, int]:
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
        logger.info("%s page %d: +%d (total %d)", url.split("/")[-1], page, len(batch), len(rows))
        if not nxt:
            return rows, False, page
        if page >= max_pages:
            return rows, True, page
        cursor = nxt
        time.sleep(delay)


def score_channel(
    name: str,
    rows: list[dict],
    capped: bool,
    pages: int,
    dates: set[str],
    extra: dict | None = None,
) -> dict:
    in_window = {d for d in dates if MIN_D <= d <= MAX_D}
    covered = sorted(KNOWN_STALE & in_window)
    missed = sorted(KNOWN_STALE - in_window)
    is_firehose = len(in_window) > FIREHOSE_DATE_LIMIT or (
        capped and len(in_window) > FIREHOSE_DATE_LIMIT // 2
    )
    usable = (not is_firehose) and len(covered) >= 2  # at least 2 of 3 known stale
    out = {
        "channel": name,
        "rows": len(rows),
        "pages": pages,
        "capped": capped,
        "distinct_dates_all": len(dates),
        "distinct_dates_in_window": len(in_window),
        "dates_in_window": sorted(in_window),
        "dates_in_window_sample": sorted(in_window)[:50],
        "known_stale_covered": covered,
        "known_stale_missed": missed,
        "is_firehose": is_firehose,
        "usable_for_discovery": usable,
        "min_date": min(dates) if dates else None,
        "max_date": max(dates) if dates else None,
    }
    if extra:
        out.update(extra)
    return out


def probe_timecards(client: HCSSClient, ms: str, delay: float) -> dict:
    print(f"\n{'='*72}\nA. GET timeCardInfo?modifiedSince={ms}\n{'='*72}")
    rows, capped, pages = paginate_get(
        client,
        _TIMECARDS,
        {"modifiedSince": ms, "limit": 1000},
        max_pages=MAX_PAGES,
        delay=delay,
    )
    dates = {_day(r.get("date")) for r in rows}
    dates.discard(None)
    # Also collect lastModified for sanity
    mods = Counter(str(r.get("lastModifiedPreciseDateTime") or "")[:10] for r in rows)
    # Jobs of interest
    watch_jobs = {
        "25006", "25048", "25060CONST", "25069C", "26004"
    }  # job codes not on timeCardInfo — only jobId. Just report dates.
    result = score_channel("timeCardInfo", rows, capped, pages, dates, {
        "sample_fields": sorted(rows[0].keys()) if rows else [],
        "top_last_modified_days": mods.most_common(10),
        "sample_rows": [
            {
                "id": r.get("id"),
                "jobId": r.get("jobId"),
                "date": _day(r.get("date")),
                "revision": r.get("revision"),
                "lastModified": r.get("lastModifiedPreciseDateTime"),
            }
            for r in rows[:15]
        ],
    })
    print(json.dumps({k: result[k] for k in (
        "rows", "capped", "distinct_dates_in_window", "known_stale_covered",
        "known_stale_missed", "is_firehose", "usable_for_discovery",
        "min_date", "max_date",
    )}, indent=2))
    return result


def probe_cc_txn(client: HCSSClient, ms: str, delay: float) -> dict:
    print(f"\n{'='*72}\nB. POST costCodeTimeCardTransactions {{modifiedSince={ms}}}\n{'='*72}")
    rows, capped, pages = paginate_post(
        client,
        _CC_TXN,
        {"modifiedSince": ms, "limit": 500},
        max_pages=MAX_PAGES,
        delay=delay,
    )
    dates: set[str] = set()
    watch_hits: list[dict] = []
    qty_nonzero = 0
    for r in rows:
        d = _day(r.get("date"))
        if d:
            dates.add(d)
        job = r.get("job") or {}
        cc = r.get("costCode") or {}
        jcode = (job.get("jobCode") or "").upper()
        ccode = (cc.get("costCodeCode") or "").upper()
        qty = _sf(r.get("quantity"))
        if qty != 0:
            qty_nonzero += 1
        if (jcode, ccode) in WATCH_KEYS:
            watch_hits.append({
                "date": d,
                "job_code": jcode,
                "cost_code": ccode,
                "quantity": qty,
                "syncTimestamp": r.get("syncTimestamp"),
                "timeCardId": r.get("timeCardId"),
            })
    result = score_channel("costCodeTimeCardTransactions", rows, capped, pages, dates, {
        "sample_fields": sorted(rows[0].keys()) if rows else [],
        "qty_nonzero_rows": qty_nonzero,
        "watchlist_hit_count": len(watch_hits),
        "watchlist_hits": watch_hits[:40],
        "watchlist_txn_dates": sorted({h["date"] for h in watch_hits if h["date"]}),
        "sample_rows": [
            {
                "date": _day(r.get("date")),
                "job": (r.get("job") or {}).get("jobCode"),
                "costCode": (r.get("costCode") or {}).get("costCodeCode"),
                "quantity": r.get("quantity"),
                "syncTimestamp": r.get("syncTimestamp"),
            }
            for r in rows[:15]
        ],
    })
    print(json.dumps({k: result[k] for k in (
        "rows", "capped", "distinct_dates_in_window", "known_stale_covered",
        "known_stale_missed", "is_firehose", "usable_for_discovery",
        "qty_nonzero_rows", "watchlist_hit_count", "watchlist_txn_dates",
        "min_date", "max_date",
    )}, indent=2, default=str))
    return result


def probe_jobcosts_v2(client: HCSSClient, ms: str, delay: float) -> dict:
    print(f"\n{'='*72}\nC. POST v2/jobCosts/advancedRequest {{modifiedSince={ms}}}\n{'='*72}")
    rows, capped, pages = paginate_post(
        client,
        _JOB_COSTS_V2,
        {"modifiedSince": ms, "limit": 500},
        max_pages=MAX_PAGES,
        delay=delay,
    )
    dates: set[str] = set()
    entry_types = Counter()
    for r in rows:
        d = _day(r.get("date"))
        if d:
            dates.add(d)
        entry_types[r.get("entryType") or ""] += 1
    result = score_channel("jobCosts_v2", rows, capped, pages, dates, {
        "sample_fields": sorted(rows[0].keys()) if rows else [],
        "entry_types": dict(entry_types),
        "sample_rows": [
            {
                "date": _day(r.get("date")),
                "job": (r.get("job") or {}).get("jobCode"),
                "costCode": (r.get("costCode") or {}).get("costCodeCode"),
                "quantity": r.get("quantity"),
                "entryType": r.get("entryType"),
            }
            for r in rows[:15]
        ],
    })
    print(json.dumps({k: result[k] for k in (
        "rows", "capped", "distinct_dates_in_window", "known_stale_covered",
        "known_stale_missed", "is_firehose", "usable_for_discovery",
        "entry_types", "min_date", "max_date",
    )}, indent=2, default=str))
    return result


def corrupt_known_stale_days(db) -> int:
    """
    Intentionally zero installed_quantity on known stale days for watchlist
    cost codes so we can prove discovery+refresh restores them.
    """
    n = 0
    rows = (
        db.query(QuantityTest)
        .filter(QuantityTest.tracking_date.in_(sorted(KNOWN_STALE)))
        .all()
    )
    for r in rows:
        key = ((r.job_code or "").upper(), (r.cost_code or "").upper())
        if key not in WATCH_KEYS:
            continue
        if float(r.installed_quantity or 0) == 0:
            continue
        r.installed_quantity = 0.0
        r.percent_complete = 0.0
        r.status = "ON_TRACK"
        r.alert = False
        n += 1
    db.commit()
    return n


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--modified-since",
        default="",
        help="ISO datetime. Default: 2026-07-19T00:00:00Z (before known edits)",
    )
    ap.add_argument("--delay", type=float, default=0.4)
    ap.add_argument(
        "--skip-refresh",
        action="store_true",
        help="Only probe discovery channels; do not corrupt/refresh",
    )
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    create_tables()
    if args.modified_since:
        raw = args.modified_since.replace("Z", "+00:00")
        ms_dt = datetime.fromisoformat(raw)
        if ms_dt.tzinfo is None:
            ms_dt = ms_dt.replace(tzinfo=timezone.utc)
    else:
        # Day before the overnight drift window — should catch Jul15/17/20 edits
        ms_dt = datetime(2026, 7, 19, 0, 0, 0, tzinfo=timezone.utc)
    ms = ms_dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    print("=" * 72)
    print("QUANTITY DIRTY-CHANNEL THOROUGH TEST")
    print("=" * 72)
    print(f"  modifiedSince : {ms}")
    print(f"  known stale   : {sorted(KNOWN_STALE)}")
    print(f"  window        : {MIN_D} -> {MAX_D}")
    print(f"  firehose if   : >{FIREHOSE_DATE_LIMIT} distinct dates in window")

    client = HCSSClient()
    report: dict[str, Any] = {
        "run_at": datetime.now(timezone.utc).isoformat(),
        "modified_since": ms,
        "known_stale": sorted(KNOWN_STALE),
        "window": {"min": MIN_D, "max": MAX_D},
        "pass_criteria": {
            "non_firehose": f"<= {FIREHOSE_DATE_LIMIT} distinct dates in window",
            "covers_known_stale": "at least 2 of 3 known stale days",
            "refresh_restores_watchlist": "after corrupt+refresh, gaps <= 0.01",
        },
    }

    # ---- Discovery probes ----------------------------------------------------
    a = probe_timecards(client, ms, args.delay)
    b = probe_cc_txn(client, ms, args.delay)
    c = probe_jobcosts_v2(client, ms, args.delay)
    report["channels"] = {"timeCardInfo": a, "costCodeTimeCardTransactions": b, "jobCosts_v2": c}

    usable = [
        ch for ch in (a, b, c) if ch.get("usable_for_discovery")
    ]
    # Prefer CC txn > timecards > jobCosts_v2 for date set
    preferred = None
    for ch in (b, a, c):
        if ch.get("usable_for_discovery"):
            preferred = ch
            break
    # Even if not "usable", pick best covering non-firehose
    if not preferred:
        candidates = [ch for ch in (a, b, c) if not ch.get("is_firehose")]
        if candidates:
            preferred = max(
                candidates,
                key=lambda ch: len(ch.get("known_stale_covered") or []),
            )

    dirty_dates: set[str] = set()
    if preferred:
        # Reconstruct dates from sample + covered — better: re-derive from channel
        dirty_dates = set(preferred.get("dates_in_window_sample") or [])
        # Also always include known covered
        dirty_dates |= set(preferred.get("known_stale_covered") or [])
        # For timecards / full lists we only have sample of 50. Re-fetch dates properly
        # from the channel's covered+sample is incomplete if many dates.
        # Rebuild from raw: store dates list on each channel fully.
    # Better: attach full date list during probe
    for key, ch in report["channels"].items():
        # We stored only sample — fix by re-deriving usable dirty set as union of
        # known_stale_covered from ALL non-firehose channels + their samples
        pass

    # Union of dates from all NON-firehose channels (from their samples + covered)
    union_dates: set[str] = set()
    for ch in (a, b, c):
        if ch.get("is_firehose"):
            continue
        union_dates |= set(ch.get("known_stale_covered") or [])
        union_dates |= set(ch.get("dates_in_window_sample") or [])
        # For CC txn, also watchlist txn dates
        union_dates |= set(ch.get("watchlist_txn_dates") or [])

    # Always test refresh on known stale if ANY channel covered them
    covered_any = set()
    for ch in (a, b, c):
        covered_any |= set(ch.get("known_stale_covered") or [])

    report["discovery_summary"] = {
        "usable_channels": [ch["channel"] for ch in usable],
        "preferred_channel": preferred["channel"] if preferred else None,
        "union_dirty_dates_non_firehose": sorted(union_dates),
        "known_stale_covered_by_any": sorted(covered_any),
        "known_stale_missed_by_all": sorted(KNOWN_STALE - covered_any),
    }
    print(f"\n{'='*72}\nDISCOVERY SUMMARY\n{'='*72}")
    print(json.dumps(report["discovery_summary"], indent=2))

    if args.skip_refresh:
        report["refresh"] = {"skipped": True}
        report["verdict"] = _verdict(report, None)
        p = _write(report, args.out)
        print(f"\nReport: {p}\n{_verdict(report, None)}")
        return

    # ---- Corrupt + refresh ---------------------------------------------------
    # Refresh set = known stale covered by any usable/non-firehose channel.
    # If none covered, still try union_dates (may be incomplete).
    refresh_dates = sorted(covered_any) if covered_any else sorted(
        d for d in union_dates if d in KNOWN_STALE or (MIN_D <= d <= MAX_D)
    )
    # Cap refresh to known stale + up to 10 other recent dates to keep runtime sane
    extra = sorted(d for d in union_dates if d not in KNOWN_STALE and d >= "2026-07-01")
    refresh_dates = sorted(set(list(covered_any) + extra[:10]))
    if not refresh_dates:
        refresh_dates = sorted(KNOWN_STALE)  # fallback to prove overwrite still works
        report["refresh_note"] = (
            "No discovery dates — falling back to known stale only "
            "(overwrite path already proven; discovery FAILED)"
        )

    print(f"\n{'='*72}\nREFRESH TEST\n{'='*72}")
    print(f"  Will refresh: {refresh_dates}")

    db = SessionLocal()
    try:
        before = sum_quantity_test_for_period(db, MIN_D, MAX_D)
        before_idx = {
            ((i.get("job_code") or "").upper(), (i.get("cost_code") or "").upper()): i
            for i in before
        }
        # Snapshot gaps before corrupt
        pre_corrupt = []
        for w in WATCHLIST:
            key = (w["job_code"].upper(), w["cost_code"].upper())
            it = before_idx.get(key)
            q = float(it["installed_quantity"]) if it else None
            pre_corrupt.append({
                **w, "db": q,
                "gap": round(w["hcss"] - q, 4) if q is not None else None,
            })
        report["watchlist_before_corrupt"] = pre_corrupt

        n = corrupt_known_stale_days(db)
        print(f"  Corrupted {n} watchlist daily cells on known stale days -> 0")
        report["corrupted_cells"] = n

        mid = sum_quantity_test_for_period(db, MIN_D, MAX_D)
        mid_idx = {
            ((i.get("job_code") or "").upper(), (i.get("cost_code") or "").upper()): i
            for i in mid
        }
        mid_rows = []
        for w in WATCHLIST:
            key = (w["job_code"].upper(), w["cost_code"].upper())
            it = mid_idx.get(key)
            q = float(it["installed_quantity"]) if it else None
            mid_rows.append({
                **w, "db": q,
                "gap": round(w["hcss"] - q, 4) if q is not None else None,
            })
        report["watchlist_after_corrupt"] = mid_rows
        print("  After corrupt (should show gaps):")
        for r in mid_rows:
            print(f"    {r['job_code']:<12} {r['cost_code']:<14} gap={r['gap']}")
    finally:
        db.close()

    print("\n  Fetching active jobs + cost-code meta for refresh...")
    active = fetch_active_jobs(client)
    all_ids = [j["id"] for j in active if j.get("id")]
    code_map = {
        j["id"]: (j.get("code") or j.get("jobCode") or "") for j in active if j.get("id")
    }
    meta = fetch_cost_code_meta(client, all_ids, args.delay)
    print(f"  active_jobs={len(all_ids)} cost_codes={len(meta)}")

    refreshed = []
    db = SessionLocal()
    try:
        for i, d in enumerate(refresh_dates, 1):
            print(f"  [{i}/{len(refresh_dates)}] refresh {d}...")
            try:
                installed = progress_for_day(client, all_ids, d, args.delay)
                day_rows = build_day_rows(installed, meta, code_map)
                n = store_quantity_test(db, d, day_rows)
                print(f"    stored {n}")
                refreshed.append({"date": d, "rows": n, "ok": True})
            except Exception as exc:
                logger.exception("refresh %s failed", d)
                refreshed.append({"date": d, "ok": False, "error": str(exc)})
                try:
                    db.rollback()
                except Exception:
                    pass
    finally:
        db.close()
    report["refresh"] = {"dates": refresh_dates, "results": refreshed}

    db = SessionLocal()
    try:
        after = sum_quantity_test_for_period(db, MIN_D, MAX_D)
    finally:
        db.close()
    after_idx = {
        ((i.get("job_code") or "").upper(), (i.get("cost_code") or "").upper()): i
        for i in after
    }
    after_rows = []
    match = 0
    print("\n  Watchlist AFTER refresh:")
    for w in WATCHLIST:
        key = (w["job_code"].upper(), w["cost_code"].upper())
        it = after_idx.get(key)
        q = float(it["installed_quantity"]) if it else None
        gap = round(w["hcss"] - q, 4) if q is not None else None
        st = "MATCH" if gap is not None and abs(gap) <= TOL else "GAP"
        if st == "MATCH":
            match += 1
        print(f"    {st:<6} {w['job_code']:<12} {w['cost_code']:<14} "
              f"db={q} hcss={w['hcss']} gap={gap}")
        after_rows.append({**w, "db": q, "gap": gap, "status": st})
    report["watchlist_after_refresh"] = after_rows
    report["refresh_match_count"] = match
    report["refresh_total"] = len(WATCHLIST)

    verdict = _verdict(report, match)
    report["verdict"] = verdict
    print(f"\n{'='*72}\nVERDICT\n{'='*72}\n{verdict}")
    p = _write(report, args.out)
    print(f"\nReport: {p}")


def _verdict(report: dict, match: int | None) -> str:
    chans = report.get("channels") or {}
    usable = [k for k, v in chans.items() if v.get("usable_for_discovery")]
    covered = set(report.get("discovery_summary", {}).get("known_stale_covered_by_any") or [])
    missed = KNOWN_STALE - covered
    firehose_all = all(v.get("is_firehose") for v in chans.values()) if chans else True

    lines = []
    if usable:
        lines.append(
            f"DISCOVERY: PASS on {usable} — non-firehose and covers known stale days."
        )
    elif covered and not firehose_all:
        lines.append(
            f"DISCOVERY: PARTIAL — covered {sorted(covered)}, missed {sorted(missed)}. "
            "May work with lookback overlap."
        )
    elif firehose_all:
        lines.append(
            "DISCOVERY: FAIL — all channels behave like firehose or miss known stale."
        )
    else:
        lines.append(
            f"DISCOVERY: FAIL — known stale missed={sorted(missed)}."
        )

    if match is not None:
        if match == len(WATCHLIST):
            lines.append(
                f"REFRESH: PASS — {match}/{len(WATCHLIST)} watchlist rows match HCSS "
                "after discovery-driven refresh."
            )
        else:
            lines.append(
                f"REFRESH: FAIL — only {match}/{len(WATCHLIST)} match after refresh."
            )

    # Final go/no-go
    go = bool(usable) and (match is None or match == len(WATCHLIST))
    partial_go = (
        len(covered) >= 2
        and not firehose_all
        and (match is None or match == len(WATCHLIST))
    )
    if go:
        lines.append(
            "GO FOR MAIN CODE: Yes — use preferred non-firehose channel(s) + "
            "7-day lookback overlap + progress(day) overwrite."
        )
    elif partial_go:
        lines.append(
            "CONDITIONAL GO: Discovery covers most known stale days and refresh works. "
            "Use channel union + lookback; monitor missed edge cases."
        )
    else:
        lines.append(
            "NO-GO for modifiedSince-only dirty sync. Fall back to lookback "
            "(e.g. 30 days) + periodic full reconcile."
        )
    return "\n".join(lines)


def _write(report: dict, out: str) -> Path:
    if out:
        p = Path(out)
    else:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        p = _REPO / "reports" / f"quantity_dirty_channels_{ts}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    return p


if __name__ == "__main__":
    main()
