"""
populate_quantity_test.py
────────────────────────────────────────────────────────────────────────────────
Populate the isolated `quantity_test` table using the proposed daily model:

  For each calendar day D in [--start, --end]:
    1. Use a FIXED list of HCSS active jobs (status=active, not deleted)
    2. Cost-code metadata (planned qty) fetched once for those jobs
    3. Progress for day D only: POST costCode/progress/advancedRequest
       with startDate=D endDate=D → SUM(quantity) per cost code
    4. Keep rows with planned_quantity > 0  (Issue 2 filter NOT changed yet)
    5. Overwrite that day's rows in `quantity_test` only

Does NOT write to quantity_tracking_cache / quantity_tracking_results.
Does NOT touch production cron or /api/quantity/* routes.

USAGE:
  python scripts/populate_quantity_test.py --start 2026-06-01 --end 2026-06-07
  python scripts/populate_quantity_test.py --start 2026-05-01 --end 2026-07-17 --overwrite
  python scripts/populate_quantity_test.py --start 2026-06-01 --end 2026-06-07 --job-code 25006,25071
  python scripts/populate_quantity_test.py --start 2026-06-01 --end 2026-06-03 --dry-run

OPTIONS:
  --start / --end   Required date range YYYY-MM-DD
  --job-code        Comma-separated job codes to limit (optional)
  --job-id          Comma-separated job UUIDs to limit (optional)
  --overwrite       Re-fetch and replace days that already have rows
  --dry-run         Fetch from HCSS but do not write to DB
  --delay           Seconds between HCSS progress calls (default 0.35)
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from collections import defaultdict
from datetime import date, timedelta
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
logger = logging.getLogger("populate_quantity_test")

from payroll_verification.hcss_client import HCSSClient  # noqa: E402
from api.database import SessionLocal, create_tables  # noqa: E402
from api.db_services import (  # noqa: E402
    quantity_test_has_data,
    store_quantity_test,
)

_BASE = "https://api.hcssapps.com/heavyjob/api/v1"
_JOBS = f"{_BASE}/jobs"
_PROGRESS = f"{_BASE}/costCode/progress/advancedRequest"
_COST_CODES_V2 = "https://api.hcssapps.com/heavyjob/api/v2/costCodes/advancedRequest"
_JOBS_ADV = f"{_BASE}/jobs/advanced"

ALERT_THRESHOLD = 75.0


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


def _determine_cost_type(cost_code: dict[str, Any]) -> str:
    raw = (
        cost_code.get("costType")
        or cost_code.get("type")
        or cost_code.get("workType")
        or ""
    ).lower()
    return "subcontractor" if "sub" in raw else "self_perform"


def _status(percent: float) -> str:
    if percent >= 100.0:
        return "OVER_RISK"
    if percent >= ALERT_THRESHOLD:
        return "NEAR_COMPLETION"
    return "ON_TRACK"


def fetch_active_jobs(client: HCSSClient) -> list[dict]:
    """All HCSS jobs with status=active and not deleted."""
    raw = _paginate_get(client, _JOBS, {"limit": 500})
    active = [
        j
        for j in raw
        if str(j.get("status") or "").lower() == "active"
        and not j.get("isDeleted", False)
    ]
    return active


def resolve_job_codes(
    client: HCSSClient, job_ids: list[str]
) -> dict[str, str]:
    """job_id → job_code."""
    if not job_ids:
        return {}
    out: dict[str, str] = {}
    # Prefer codes already on the jobs list; fill gaps via jobs/advanced
    try:
        rows = _paginate_post(client, _JOBS_ADV, {"jobIds": job_ids, "limit": 500})
        for r in rows:
            jid = r.get("id") or r.get("jobId") or ""
            if jid:
                out[jid] = r.get("jobCode") or r.get("code") or ""
    except Exception as exc:
        logger.warning("jobs/advanced failed: %s", exc)
    return out


def fetch_cost_code_meta(
    client: HCSSClient, job_ids: list[str], delay: float
) -> dict[str, dict]:
    """costCodeId → metadata including planned qty and raw cost-code dict."""
    meta: dict[str, dict] = {}
    if not job_ids:
        return meta
    BATCH = 50
    for i in range(0, len(job_ids), BATCH):
        batch = job_ids[i : i + BATCH]
        rows = _paginate_post(
            client, _COST_CODES_V2, {"jobIds": batch, "limit": 500}
        )
        for cc in rows:
            ccid = cc.get("id") or cc.get("costCodeId") or ""
            if not ccid:
                continue
            meta[ccid] = {
                "raw": cc,
                "cost_code": cc.get("code") or cc.get("costCode") or ccid[:8],
                "description": (
                    cc.get("description")
                    or cc.get("name")
                    or "No description"
                ),
                "planned": _sf(
                    cc.get("quantity")
                    or cc.get("plannedQuantity")
                    or cc.get("budgetQuantity")
                    or cc.get("budgetedQuantity")
                ),
                "job_id": cc.get("jobId") or cc.get("job_id") or "",
                "unit": (
                    cc.get("unitOfMeasure")
                    or cc.get("unit")
                    or cc.get("uom")
                    or "UNIT"
                ),
                "cost_type": _determine_cost_type(cc),
            }
        time.sleep(delay)
    return meta


def progress_for_day(
    client: HCSSClient, job_ids: list[str], day: str, delay: float
) -> dict[str, float]:
    """costCodeId → SUM(quantity) for that single day across jobs."""
    totals: dict[str, float] = defaultdict(float)
    for jid in job_ids:
        rows = _paginate_post(
            client,
            _PROGRESS,
            {
                "jobId": jid,
                "startDate": f"{day}T00:00:00Z",
                "endDate": f"{day}T23:59:59Z",
                "limit": 500,
            },
        )
        for p in rows:
            ccid = p.get("costCodeId") or p.get("id") or ""
            if not ccid:
                continue
            totals[ccid] += _sf(p.get("quantity"))
        time.sleep(delay)
    return dict(totals)


def build_day_rows(
    installed_map: dict[str, float],
    cc_meta: dict[str, dict],
    job_code_map: dict[str, str],
) -> list[dict]:
    """
    Join cost-code meta + that day's installed qty.
    Stores all cost codes (including planned <= 0) — same as production tracker.
    """
    results: list[dict] = []
    for ccid, meta in cc_meta.items():
        planned = float(meta["planned"])
        installed = round(float(installed_map.get(ccid, 0.0)), 4)
        percent = round((installed / planned) * 100, 2) if planned > 0 else 0.0
        jid = meta["job_id"]
        results.append({
            "cost_code_id": ccid,
            "cost_code": meta["cost_code"],
            "description": meta["description"],
            "job_id": jid,
            "job_code": job_code_map.get(jid) or jid[:8],
            "unit": meta["unit"],
            "cost_type": meta["cost_type"],
            "planned_quantity": planned,
            "installed_quantity": installed,
            "remaining_quantity": round(planned - installed, 4),
            "percent_complete": percent,
            "status": _status(percent),
            "alert": percent >= ALERT_THRESHOLD,
        })
    return results


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Populate quantity_test table with daily active-jobs progress"
    )
    parser.add_argument("--start", required=True, help="Start date YYYY-MM-DD")
    parser.add_argument("--end", required=True, help="End date YYYY-MM-DD")
    parser.add_argument("--job-code", default="", help="Comma-separated job codes")
    parser.add_argument("--job-id", default="", help="Comma-separated job UUIDs")
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Re-fetch days that already have quantity_test rows",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch from HCSS but do not write to DB",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=0.35,
        help="Seconds between HCSS progress calls (default 0.35)",
    )
    args = parser.parse_args()

    start, end = args.start, args.end
    date.fromisoformat(start)
    date.fromisoformat(end)
    if start > end:
        raise SystemExit("--start must be <= --end")

    days = _date_list(start, end)
    code_filter = {c.strip().upper() for c in args.job_code.split(",") if c.strip()}
    id_filter = {c.strip() for c in args.job_id.split(",") if c.strip()}

    create_tables()
    client = HCSSClient()

    print(f"\n{'=' * 72}")
    print("POPULATE quantity_test (daily active-jobs model)")
    print(f"{'=' * 72}")
    print(f"  Range     : {start} → {end}  ({len(days)} day(s))")
    print(f"  Overwrite : {args.overwrite}")
    print(f"  Dry-run   : {args.dry_run}")
    print(f"  Filter    : job-code={sorted(code_filter) or 'all'} "
          f"job-id={sorted(id_filter) or 'all'}")
    print()

    print("[1] Fetching active HCSS jobs…")
    active = fetch_active_jobs(client)
    print(f"  {len(active)} active job(s)")

    jobs: list[dict] = []
    for j in active:
        jid = j.get("id") or ""
        if not jid:
            continue
        code = (j.get("code") or j.get("jobCode") or "").strip()
        if id_filter and jid not in id_filter:
            continue
        if code_filter and code.upper() not in code_filter:
            continue
        jobs.append(j)

    if not jobs:
        raise SystemExit("No active jobs matched filters.")

    job_ids = [j["id"] for j in jobs]
    job_code_map = {
        j["id"]: (j.get("code") or j.get("jobCode") or "") for j in jobs
    }
    # Fill any missing codes
    missing = [jid for jid in job_ids if not job_code_map.get(jid)]
    if missing:
        resolved = resolve_job_codes(client, missing)
        job_code_map.update(resolved)

    print(f"  Using {len(job_ids)} job(s):")
    for jid in job_ids:
        print(f"    • {job_code_map.get(jid) or jid[:8]}  ({jid})")

    print("\n[2] Fetching cost-code metadata (planned qty)…")
    cc_meta = fetch_cost_code_meta(client, job_ids, args.delay)
    planned_ok = sum(1 for m in cc_meta.values() if m["planned"] > 0)
    print(f"  {len(cc_meta)} cost code(s), {planned_ok} with planned > 0")

    db = SessionLocal()
    ok = skip = fail = 0
    try:
        for i, day in enumerate(days, 1):
            print(f"\n[{i}/{len(days)}] Day {day}")
            if not args.overwrite and quantity_test_has_data(db, day):
                print("  SKIP — already has quantity_test rows (use --overwrite)")
                skip += 1
                continue
            try:
                installed = progress_for_day(client, job_ids, day, args.delay)
                rows = build_day_rows(installed, cc_meta, job_code_map)
                nonzero = sum(1 for r in rows if r["installed_quantity"] != 0)
                print(
                    f"  Built {len(rows)} row(s) "
                    f"({nonzero} with installed ≠ 0)"
                )
                if args.dry_run:
                    print("  DRY-RUN — not writing")
                    ok += 1
                    continue
                n = store_quantity_test(db, day, rows)
                print(f"  Stored {n} row(s) in quantity_test")
                ok += 1
            except Exception as exc:
                logger.exception("Failed day %s: %s", day, exc)
                print(f"  FAIL — {exc}")
                fail += 1
                try:
                    db.rollback()
                except Exception:
                    pass
    finally:
        db.close()

    print(f"\n{'=' * 72}")
    print(f"Done. ok={ok}  skipped={skip}  failed={fail}")
    print("Read via: GET /api/quantity-test/verify?start_date=...&end_date=...")
    print(f"{'=' * 72}\n")


if __name__ == "__main__":
    main()
