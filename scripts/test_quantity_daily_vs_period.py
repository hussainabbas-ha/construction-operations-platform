"""
test_quantity_daily_vs_period.py
────────────────────────────────────────────────────────────────────────────────
Prove whether Issue 1's "daily table + sum on read" fix is safe BEFORE wiring
it into quantity_routes / cron.

Compares, for the same date range:

  A. HCSS PERIOD   — one progress call per job for start→end,
                     SUM(progress.quantity) per cost code
                     (= Cost Code Summary Actual Quantity)

  B1. HCSS DAY-SUM (timecard jobs) — for each calendar day, only jobs that had
      a timecard that day, then SUM daily quantity
      (= current tracker discovery style)

  B2. HCSS DAY-SUM (fixed job list) — same jobs as the period run, progress
      fetched day-by-day, then SUM
      (= proposed daily model with better job discovery)

  C.  Dashboard API  — GET /api/quantity/verify?start_date&end_date
      (= current period cache / live path)

  D.  DB daily keys  — sum quantity_tracking_results for each YYYY-MM-DD
      in the range (if those days exist in your DB)

Pass criteria for implementing daily+sum:
  A ≈ B2  (tolerance 0.01)  → daily sum is mathematically safe
  A ≠ B1 but A ≈ B2         → timecard-only discovery is the gap; fix discovery
  A ≠ C                     → period cache / API is stale or filtered (Issue 1 / 2)

USAGE:
  python scripts/test_quantity_daily_vs_period.py --start 2026-05-01 --end 2026-07-17

  python scripts/test_quantity_daily_vs_period.py --start 2026-05-01 --end 2026-07-17 \\
      --job-code 25006,25071,11001

  python scripts/test_quantity_daily_vs_period.py --start 2026-05-01 --end 2026-07-17 \\
      --api-url http://localhost:8000 --compare-db --out qty_compare.json

  # Faster smoke test (fewer HCSS day calls):
  python scripts/test_quantity_daily_vs_period.py --start 2026-06-01 --end 2026-06-07 \\
      --job-code 25006 --skip-b1

OPTIONS:
  --start / --end     Required date range YYYY-MM-DD
  --job-code          Comma-separated job codes to limit (optional)
  --job-id            Comma-separated job UUIDs to limit (optional)
  --api-url           Base URL for /api/quantity/verify (optional)
  --compare-db        Also sum daily rows from local quantity_tracking_results
  --skip-b1           Skip timecard-per-day variant (faster)
  --skip-b2           Skip fixed-job day-sum (not recommended)
  --tolerance         Diff tolerance (default 0.01)
  --delay             Seconds between HCSS calls (default 0.35)
  --out               Write full JSON report
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from dotenv import load_dotenv

load_dotenv(_REPO_ROOT / ".env")

from payroll_verification.hcss_client import HCSSClient  # noqa: E402

_BASE = "https://api.hcssapps.com/heavyjob/api/v1"
_TIMECARDS = f"{_BASE}/timeCardInfo"
_JOBS_ADV = f"{_BASE}/jobs/advanced"
_PROGRESS = f"{_BASE}/costCode/progress/advancedRequest"
_COST_CODES_V2 = "https://api.hcssapps.com/heavyjob/api/v2/costCodes/advancedRequest"

GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
CYAN = "\033[96m"
BOLD = "\033[1m"
RESET = "\033[0m"


# ── helpers ───────────────────────────────────────────────────────────────────

def _sf(val: Any) -> float:
    try:
        return float(val or 0)
    except (TypeError, ValueError):
        return 0.0


def _date_list(start: str, end: str) -> list[str]:
    cur = date.fromisoformat(start)
    last = date.fromisoformat(end)
    out: list[str] = []
    while cur <= last:
        out.append(cur.isoformat())
        cur += timedelta(days=1)
    return out


def _paginate_get(client: HCSSClient, url: str, params: dict) -> list[dict]:
    rows: list[dict] = []
    cursor = None
    while True:
        p = dict(params)
        if cursor:
            p["cursor"] = cursor
        raw = client._request("GET", url, params=p)
        batch, next_cursor = client._extract_results_and_cursor(raw)
        rows.extend(batch)
        if not next_cursor:
            break
        cursor = next_cursor
    return rows


def _paginate_post(client: HCSSClient, url: str, payload: dict) -> list[dict]:
    rows: list[dict] = []
    cursor = None
    while True:
        body = dict(payload)
        if cursor:
            body["cursor"] = cursor
        raw = client._request("POST", url, json=body)
        batch, next_cursor = client._extract_results_and_cursor(raw)
        rows.extend(batch)
        if not next_cursor:
            break
        cursor = next_cursor
    return rows


def _progress_payload(job_id: str, start: str, end: str) -> dict:
    return {
        "jobId": job_id,
        "startDate": f"{start}T00:00:00Z",
        "endDate": f"{end}T23:59:59Z",
        "limit": 500,
    }


def _sum_progress(rows: list[dict]) -> dict[str, float]:
    """costCodeId → SUM(quantity)."""
    agg: dict[str, float] = defaultdict(float)
    for r in rows:
        ccid = r.get("costCodeId") or ""
        if not ccid:
            continue
        agg[ccid] += _sf(r.get("quantity"))
    return dict(agg)


# ── HCSS discovery ────────────────────────────────────────────────────────────

def fetch_timecard_job_ids(
    client: HCSSClient, start: str, end: str
) -> list[str]:
    rows = _paginate_get(client, _TIMECARDS, {"startDate": start, "endDate": end})
    ids: dict[str, bool] = {}
    for tc in rows:
        jid = tc.get("jobId") or tc.get("job_id") or ""
        if jid:
            ids[jid] = True
    return list(ids.keys())


def resolve_job_map(client: HCSSClient, job_ids: list[str]) -> dict[str, dict]:
    if not job_ids:
        return {}
    try:
        rows = _paginate_post(client, _JOBS_ADV, {"jobIds": job_ids, "limit": 500})
    except Exception as exc:
        print(f"  {YELLOW}WARNING: jobs/advanced failed: {exc}{RESET}")
        return {}
    out: dict[str, dict] = {}
    for r in rows:
        jid = r.get("id") or r.get("jobId") or ""
        if not jid:
            continue
        out[jid] = {
            "job_code": r.get("jobCode") or r.get("code") or "",
            "job_description": r.get("description") or r.get("jobDescription") or "",
        }
    return out


def filter_job_ids(
    job_ids: list[str],
    job_map: dict[str, dict],
    codes: set[str] | None,
    ids: set[str] | None,
) -> list[str]:
    if ids:
        return [j for j in job_ids if j in ids]
    if codes:
        wanted = {c.upper() for c in codes}
        return [
            j
            for j in job_ids
            if (job_map.get(j) or {}).get("job_code", "").upper() in wanted
        ]
    return job_ids


def fetch_cost_code_meta(
    client: HCSSClient, job_ids: list[str], delay: float
) -> dict[str, dict]:
    """costCodeId → {code, description, planned, job_id, unit}."""
    meta: dict[str, dict] = {}
    if not job_ids:
        return meta
    rows = _paginate_post(
        client, _COST_CODES_V2, {"jobIds": job_ids, "limit": 500}
    )
    for cc in rows:
        ccid = cc.get("id") or cc.get("costCodeId") or ""
        if not ccid:
            continue
        meta[ccid] = {
            "cost_code": cc.get("code") or "",
            "description": cc.get("description") or "",
            "planned": _sf(
                cc.get("quantity")
                or cc.get("plannedQuantity")
                or cc.get("budgetQuantity")
            ),
            "job_id": cc.get("jobId") or cc.get("job_id") or "",
            "unit": cc.get("unitOfMeasure") or cc.get("unit") or "",
        }
    time.sleep(delay)
    return meta


# ── Comparisons A / B1 / B2 ───────────────────────────────────────────────────

def build_period_map(
    client: HCSSClient,
    job_ids: list[str],
    start: str,
    end: str,
    delay: float,
) -> dict[str, float]:
    """A: period SUM(quantity) keyed by costCodeId."""
    totals: dict[str, float] = defaultdict(float)
    for i, jid in enumerate(job_ids, 1):
        print(f"    A period progress [{i}/{len(job_ids)}] {jid[:8]}…", flush=True)
        rows = _paginate_post(client, _PROGRESS, _progress_payload(jid, start, end))
        for ccid, qty in _sum_progress(rows).items():
            totals[ccid] += qty
        time.sleep(delay)
    return dict(totals)


def build_day_sum_fixed_jobs(
    client: HCSSClient,
    job_ids: list[str],
    days: list[str],
    delay: float,
) -> dict[str, float]:
    """B2: for each day, progress for the FIXED job list, then sum."""
    totals: dict[str, float] = defaultdict(float)
    for di, day in enumerate(days, 1):
        print(
            f"    B2 day-sum fixed-jobs [{di}/{len(days)}] {day} "
            f"({len(job_ids)} jobs)",
            flush=True,
        )
        for jid in job_ids:
            rows = _paginate_post(client, _PROGRESS, _progress_payload(jid, day, day))
            for ccid, qty in _sum_progress(rows).items():
                totals[ccid] += qty
            time.sleep(delay)
    return dict(totals)


def build_day_sum_timecard_jobs(
    client: HCSSClient,
    days: list[str],
    allowed_job_ids: set[str] | None,
    delay: float,
) -> dict[str, float]:
    """B1: each day only jobs with timecards that day (current tracker style)."""
    totals: dict[str, float] = defaultdict(float)
    for di, day in enumerate(days, 1):
        day_jobs = fetch_timecard_job_ids(client, day, day)
        if allowed_job_ids is not None:
            day_jobs = [j for j in day_jobs if j in allowed_job_ids]
        print(
            f"    B1 day-sum timecard-jobs [{di}/{len(days)}] {day} "
            f"→ {len(day_jobs)} jobs",
            flush=True,
        )
        for jid in day_jobs:
            rows = _paginate_post(client, _PROGRESS, _progress_payload(jid, day, day))
            for ccid, qty in _sum_progress(rows).items():
                totals[ccid] += qty
            time.sleep(delay)
    return dict(totals)


# ── API + DB ──────────────────────────────────────────────────────────────────

def fetch_api_map(
    api_url: str, start: str, end: str
) -> tuple[dict[tuple[str, str], float], dict[str, Any]]:
    """
    (job_id, cost_code_id) → installed_quantity
    Also returns raw response meta.
    """
    import requests

    url = f"{api_url.rstrip('/')}/api/quantity/verify"
    resp = requests.get(
        url,
        params={"start_date": start, "end_date": end},
        timeout=120,
    )
    meta = {"status_code": resp.status_code, "url": url}
    if resp.status_code == 404:
        meta["detail"] = "404 — no cached/period data"
        return {}, meta
    resp.raise_for_status()
    data = resp.json()
    meta["total"] = data.get("total")
    meta["period_start"] = data.get("period_start")
    meta["period_end"] = data.get("period_end")
    out: dict[tuple[str, str], float] = {}
    for item in data.get("items") or []:
        key = (item.get("job_id") or "", item.get("cost_code_id") or "")
        if key[1]:
            out[key] = _sf(item.get("installed_quantity"))
    return out, meta


def fetch_db_daily_sum(
    start: str, end: str, allowed_job_ids: set[str] | None
) -> tuple[dict[tuple[str, str], float], dict[str, Any]]:
    """Sum installed_quantity from daily tracking_date keys only (not start__end)."""
    from api.database import SessionLocal, QuantityTrackingResult, QuantityTrackingCache

    days = _date_list(start, end)
    db = SessionLocal()
    meta: dict[str, Any] = {"days_requested": len(days), "days_with_rows": 0}
    try:
        # Period cache info (Issue 1 smoking gun)
        period_key = start if start == end else f"{start}__{end}"
        period = (
            db.query(QuantityTrackingCache)
            .filter(QuantityTrackingCache.tracking_date == period_key)
            .first()
        )
        if period:
            meta["period_cache_key"] = period_key
            meta["period_fetched_at"] = (
                period.fetched_at.isoformat() if period.fetched_at else None
            )
            meta["period_total_cost_codes"] = period.total_cost_codes
        else:
            meta["period_cache_key"] = period_key
            meta["period_fetched_at"] = None

        totals: dict[tuple[str, str], float] = defaultdict(float)
        days_hit = 0
        for d in days:
            rows = (
                db.query(QuantityTrackingResult)
                .filter(QuantityTrackingResult.tracking_date == d)
                .all()
            )
            if not rows:
                continue
            days_hit += 1
            for r in rows:
                if allowed_job_ids is not None and r.job_id not in allowed_job_ids:
                    continue
                totals[(r.job_id, r.cost_code_id)] += _sf(r.installed_quantity)
        meta["days_with_rows"] = days_hit
        return dict(totals), meta
    finally:
        db.close()


# ── report ────────────────────────────────────────────────────────────────────

def _index_by_ccid(
    qty_by_ccid: dict[str, float],
    meta: dict[str, dict],
) -> dict[tuple[str, str], float]:
    """Map costCodeId totals → (job_id, cost_code_id) using cost-code meta."""
    out: dict[tuple[str, str], float] = {}
    for ccid, qty in qty_by_ccid.items():
        jid = (meta.get(ccid) or {}).get("job_id") or ""
        out[(jid, ccid)] = qty
    return out


def diff_maps(
    left: dict[tuple[str, str], float],
    right: dict[tuple[str, str], float],
    label_left: str,
    label_right: str,
    meta: dict[str, dict],
    job_map: dict[str, dict],
    tolerance: float,
) -> list[dict]:
    keys = set(left) | set(right)
    diffs: list[dict] = []
    for key in sorted(keys, key=lambda k: (k[0], k[1])):
        lv = left.get(key, 0.0)
        rv = right.get(key, 0.0)
        delta = round(lv - rv, 4)
        if abs(delta) <= tolerance:
            continue
        jid, ccid = key
        cc = meta.get(ccid) or {}
        jm = job_map.get(jid) or {}
        diffs.append(
            {
                "job_id": jid,
                "job_code": jm.get("job_code") or cc.get("job_id", "")[:8],
                "cost_code_id": ccid,
                "cost_code": cc.get("cost_code", ""),
                "description": cc.get("description", ""),
                "planned": cc.get("planned", 0.0),
                label_left: round(lv, 4),
                label_right: round(rv, 4),
                "delta": delta,
            }
        )
    return diffs


def print_diff_table(title: str, diffs: list[dict], left: str, right: str) -> None:
    print(f"\n{BOLD}{title}{RESET}")
    if not diffs:
        print(f"  {GREEN}✓ No differences (within tolerance){RESET}")
        return
    print(f"  {RED}✗ {len(diffs)} cost code(s) differ{RESET}")
    print(
        f"  {'Job':<12} {'CostCode':<16} {left:>12} {right:>12} {'Delta':>10}  Description"
    )
    print("  " + "-" * 90)
    for d in diffs[:40]:
        print(
            f"  {str(d.get('job_code') or ''):<12} "
            f"{str(d.get('cost_code') or ''):<16} "
            f"{d.get(left, 0):>12,.2f} "
            f"{d.get(right, 0):>12,.2f} "
            f"{d.get('delta', 0):>10,.2f}  "
            f"{(d.get('description') or '')[:40]}"
        )
    if len(diffs) > 40:
        print(f"  … and {len(diffs) - 40} more")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Compare HCSS period vs day-sum vs dashboard/DB for quantity Issue 1"
    )
    parser.add_argument("--start", required=True, help="Start date YYYY-MM-DD")
    parser.add_argument("--end", required=True, help="End date YYYY-MM-DD")
    parser.add_argument(
        "--job-code",
        default="",
        help="Comma-separated job codes to limit (recommended for first runs)",
    )
    parser.add_argument("--job-id", default="", help="Comma-separated job UUIDs")
    parser.add_argument(
        "--api-url",
        default="",
        help="Base URL for GET /api/quantity/verify (e.g. http://localhost:8000)",
    )
    parser.add_argument(
        "--compare-db",
        action="store_true",
        help="Also sum local daily quantity_tracking_results keys",
    )
    parser.add_argument("--skip-b1", action="store_true", help="Skip timecard-per-day day-sum")
    parser.add_argument("--skip-b2", action="store_true", help="Skip fixed-job day-sum")
    parser.add_argument("--tolerance", type=float, default=0.01)
    parser.add_argument("--delay", type=float, default=0.35, help="Delay between HCSS calls")
    parser.add_argument("--out", default="", help="Write JSON report path")
    args = parser.parse_args()

    start, end = args.start, args.end
    date.fromisoformat(start)
    date.fromisoformat(end)
    if start > end:
        raise SystemExit("--start must be <= --end")

    days = _date_list(start, end)
    code_filter = {c.strip() for c in args.job_code.split(",") if c.strip()} or None
    id_filter = {c.strip() for c in args.job_id.split(",") if c.strip()} or None

    print(f"\n{BOLD}{'=' * 72}")
    print("QUANTITY: PERIOD vs DAY-SUM vs DASHBOARD/DB")
    print(f"{'=' * 72}{RESET}")
    print(f"  Range     : {start} → {end}  ({len(days)} day(s))")
    print(f"  Tolerance : {args.tolerance}")
    print(f"  Job filter: {sorted(code_filter) if code_filter else sorted(id_filter) if id_filter else 'all timecard-active in range'}")
    print()
    print("  A  = HCSS period SUM(quantity)")
    print("  B1 = HCSS day-sum (timecard jobs per day)  [current discovery]")
    print("  B2 = HCSS day-sum (fixed job list)         [proposed daily model]")
    print("  C  = Dashboard /api/quantity/verify")
    print("  D  = DB sum of daily tracking_date keys")
    print()

    client = HCSSClient()

    # Discover jobs from timecards in full range (same as period tracker entry)
    print(f"{CYAN}[1] Discover jobs via timeCardInfo for full range…{RESET}")
    all_job_ids = fetch_timecard_job_ids(client, start, end)
    print(f"  {len(all_job_ids)} job(s) with timecards in range")
    job_map = resolve_job_map(client, all_job_ids)
    job_ids = filter_job_ids(all_job_ids, job_map, code_filter, id_filter)
    if not job_ids:
        raise SystemExit("No jobs matched filters / no timecards in range.")
    print(f"  Using {len(job_ids)} job(s) after filter:")
    for jid in job_ids:
        code = (job_map.get(jid) or {}).get("job_code") or jid[:8]
        print(f"    • {code}  ({jid})")

    print(f"\n{CYAN}[2] Fetch cost-code metadata (planned qty / codes)…{RESET}")
    cc_meta = fetch_cost_code_meta(client, job_ids, args.delay)
    print(f"  {len(cc_meta)} cost code(s)")

    report: dict[str, Any] = {
        "range": {"start": start, "end": end, "days": len(days)},
        "jobs": [
            {
                "job_id": jid,
                "job_code": (job_map.get(jid) or {}).get("job_code"),
            }
            for jid in job_ids
        ],
        "tolerance": args.tolerance,
    }

    # A
    print(f"\n{CYAN}[3] A — HCSS PERIOD progress…{RESET}")
    t0 = time.time()
    map_a_cc = build_period_map(client, job_ids, start, end, args.delay)
    map_a = _index_by_ccid(map_a_cc, cc_meta)
    print(f"  {GREEN}A done{RESET}: {len(map_a_cc)} cost codes with qty  ({time.time()-t0:.1f}s)")
    report["A_period"] = {
        "cost_codes": len(map_a_cc),
        "total_installed": round(sum(map_a_cc.values()), 4),
    }

    # B2
    map_b2: dict[tuple[str, str], float] = {}
    if not args.skip_b2:
        print(f"\n{CYAN}[4] B2 — HCSS DAY-SUM with fixed job list…{RESET}")
        print(
            f"  {YELLOW}Note: {len(days)} days × {len(job_ids)} jobs HCSS calls — "
            f"use a short range or --job-code for smoke tests{RESET}"
        )
        t0 = time.time()
        map_b2_cc = build_day_sum_fixed_jobs(client, job_ids, days, args.delay)
        map_b2 = _index_by_ccid(map_b2_cc, cc_meta)
        print(f"  {GREEN}B2 done{RESET}: {len(map_b2_cc)} cost codes  ({time.time()-t0:.1f}s)")
        report["B2_day_sum_fixed_jobs"] = {
            "cost_codes": len(map_b2_cc),
            "total_installed": round(sum(map_b2_cc.values()), 4),
        }
        diffs_a_b2 = diff_maps(map_a, map_b2, "A_period", "B2_day_sum", cc_meta, job_map, args.tolerance)
        print_diff_table("A (period) vs B2 (day-sum fixed jobs)", diffs_a_b2, "A_period", "B2_day_sum")
        report["diff_A_vs_B2"] = {"count": len(diffs_a_b2), "rows": diffs_a_b2}
    else:
        print(f"\n{YELLOW}[4] B2 skipped (--skip-b2){RESET}")
        report["B2_day_sum_fixed_jobs"] = None

    # B1
    map_b1: dict[tuple[str, str], float] = {}
    if not args.skip_b1:
        print(f"\n{CYAN}[5] B1 — HCSS DAY-SUM with timecard jobs per day…{RESET}")
        t0 = time.time()
        allowed = set(job_ids)
        map_b1_cc = build_day_sum_timecard_jobs(client, days, allowed, args.delay)
        map_b1 = _index_by_ccid(map_b1_cc, cc_meta)
        print(f"  {GREEN}B1 done{RESET}: {len(map_b1_cc)} cost codes  ({time.time()-t0:.1f}s)")
        report["B1_day_sum_timecard_jobs"] = {
            "cost_codes": len(map_b1_cc),
            "total_installed": round(sum(map_b1_cc.values()), 4),
        }
        diffs_a_b1 = diff_maps(map_a, map_b1, "A_period", "B1_day_sum", cc_meta, job_map, args.tolerance)
        print_diff_table("A (period) vs B1 (day-sum timecard jobs)", diffs_a_b1, "A_period", "B1_day_sum")
        report["diff_A_vs_B1"] = {"count": len(diffs_a_b1), "rows": diffs_a_b1}
    else:
        print(f"\n{YELLOW}[5] B1 skipped (--skip-b1){RESET}")
        report["B1_day_sum_timecard_jobs"] = None

    # C API
    if args.api_url:
        print(f"\n{CYAN}[6] C — Dashboard API {args.api_url}…{RESET}")
        try:
            map_c, api_meta = fetch_api_map(args.api_url, start, end)
            report["C_api"] = {**api_meta, "cost_codes": len(map_c)}
            print(f"  status={api_meta.get('status_code')} items={len(map_c)}")
            # Align keys: API uses job_id+cost_code_id; filter to our jobs
            map_c_f = {k: v for k, v in map_c.items() if k[0] in set(job_ids) or not job_ids}
            diffs_a_c = diff_maps(map_a, map_c_f, "A_period", "C_api", cc_meta, job_map, args.tolerance)
            print_diff_table("A (period) vs C (dashboard API)", diffs_a_c, "A_period", "C_api")
            report["diff_A_vs_C"] = {"count": len(diffs_a_c), "rows": diffs_a_c}
        except Exception as exc:
            print(f"  {RED}API compare failed: {exc}{RESET}")
            report["C_api"] = {"error": str(exc)}
    else:
        print(f"\n{YELLOW}[6] C skipped (pass --api-url to compare dashboard){RESET}")

    # D DB
    if args.compare_db:
        print(f"\n{CYAN}[7] D — Sum DB daily keys…{RESET}")
        try:
            map_d, db_meta = fetch_db_daily_sum(start, end, set(job_ids))
            report["D_db_daily"] = {**db_meta, "cost_codes": len(map_d)}
            print(f"  days_with_rows={db_meta.get('days_with_rows')}/{len(days)}")
            print(
                f"  period_cache key={db_meta.get('period_cache_key')} "
                f"fetched_at={db_meta.get('period_fetched_at')}"
            )
            diffs_a_d = diff_maps(map_a, map_d, "A_period", "D_db_daily", cc_meta, job_map, args.tolerance)
            print_diff_table("A (period) vs D (DB daily sum)", diffs_a_d, "A_period", "D_db_daily")
            report["diff_A_vs_D"] = {"count": len(diffs_a_d), "rows": diffs_a_d}
        except Exception as exc:
            print(f"  {RED}DB compare failed: {exc}{RESET}")
            report["D_db_daily"] = {"error": str(exc)}
    else:
        print(f"\n{YELLOW}[7] D skipped (pass --compare-db){RESET}")

    # Verdict
    print(f"\n{BOLD}{'=' * 72}")
    print("VERDICT")
    print(f"{'=' * 72}{RESET}")

    def _count(key: str) -> int | None:
        block = report.get(key)
        if not block:
            return None
        return block.get("count")

    c_ab2 = _count("diff_A_vs_B2")
    c_ab1 = _count("diff_A_vs_B1")
    c_ac = _count("diff_A_vs_C")
    c_ad = _count("diff_A_vs_D")

    if c_ab2 is not None:
        if c_ab2 == 0:
            print(
                f"  {GREEN}✓ A ≈ B2{RESET} — day-sum with fixed jobs matches HCSS period.\n"
                f"    Safe to implement daily table + sum on read (with proper job list)."
            )
        else:
            print(
                f"  {RED}✗ A ≠ B2 ({c_ab2} diffs){RESET} — do NOT switch dashboard yet.\n"
                f"    Inspect pagination / date TZ / progress rows for those codes."
            )

    if c_ab1 is not None and c_ab2 is not None:
        if c_ab1 > 0 and c_ab2 == 0:
            print(
                f"  {YELLOW}⚠ A ≠ B1 but A ≈ B2{RESET} — timecard-only discovery is the gap.\n"
                f"    Daily model needs active/fixed jobs, not only timecards-that-day."
            )
        elif c_ab1 == 0 and c_ab2 == 0:
            print(
                f"  {GREEN}✓ A ≈ B1 ≈ B2{RESET} — for THIS range/jobs, timecard discovery was enough."
            )

    if c_ac is not None:
        if c_ac == 0:
            print(f"  {GREEN}✓ A ≈ C{RESET} — dashboard currently matches HCSS (cache may still be fresh).")
        else:
            print(
                f"  {RED}✗ A ≠ C ({c_ac} diffs){RESET} — dashboard/period cache disagrees with live HCSS.\n"
                f"    This is Issue 1 (or Issue 2 filters) showing up."
            )

    if c_ad is not None:
        if c_ad == 0:
            print(f"  {GREEN}✓ A ≈ D{RESET} — existing DB daily keys already sum to HCSS period.")
        else:
            print(
                f"  {YELLOW}⚠ A ≠ D ({c_ad} diffs){RESET} — daily DB incomplete/stale/missing days.\n"
                f"    days_with_rows may be < range; backfill dailies before trusting D."
            )

    print()
    print("  Tip: re-run after an HCSS edit on an old date inside the range.")
    print("  If A updates but C stays wrong until clear_quantity_cache → Issue 1 confirmed.")
    print()

    if args.out:
        out_path = Path(args.out)
        out_path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
        print(f"  Wrote report → {out_path.resolve()}")


if __name__ == "__main__":
    main()
