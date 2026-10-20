"""Measure jobCosts?modifiedSince volume + dirty-date coverage across windows."""
from __future__ import annotations

import json
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))

from dotenv import load_dotenv

load_dotenv(_REPO / ".env")

from payroll_verification.hcss_client import HCSSClient

_JOB_COSTS_V1 = "https://api.hcssapps.com/heavyjob/api/v1/jobCosts/advancedRequest"
KNOWN_STALE = {"2026-07-15", "2026-07-17", "2026-07-20"}
WATCH_KEYS = {
    ("25006", "80711111"), ("25006", "80011111B"), ("25006", "58011111D"),
    ("25069C", "14511111C"), ("26004", "61340010A"), ("25060CONST", "40011111"),
    ("25060CONST", "24011111A"), ("25048", "12011111C"), ("25006", "58511111C"),
    ("25006", "5111111"), ("25060CONST", "211111I"), ("25006", "60011111B"),
    ("25006", "1011111"),
}
MIN_D, MAX_D = "2026-05-01", "2026-07-20"


def _sf(v: Any) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def probe(client: HCSSClient, hours: float, max_pages: int) -> dict:
    ms = datetime.now(timezone.utc) - timedelta(hours=hours)
    ms_str = ms.strftime("%Y-%m-%dT%H:%M:%S")
    print(f"\n{'='*70}\nWINDOW: last {hours}h  modifiedSince={ms_str}")
    rows: list[dict] = []
    cursor = None
    page = 0
    capped = False
    while True:
        body = {"modifiedSince": ms_str, "limit": 500}
        if cursor:
            body["cursor"] = cursor
        raw = client._request("POST", _JOB_COSTS_V1, json=body)
        batch, nxt = client._extract_results_and_cursor(raw)
        page += 1
        rows.extend(batch)
        if page % 20 == 0:
            print(f"  page {page}: total {len(rows)}")
        if not nxt:
            break
        cursor = nxt
        if page >= max_pages:
            capped = True
            print(f"  CAPPED at {max_pages} pages ({len(rows)} rows) — set larger than cap")
            break

    dates_all = Counter()
    dates_window = set()
    watch_hits = Counter()
    watch_dates = set()
    for r in rows:
        d = str(r.get("date") or "")[:10]
        if d:
            dates_all[d] += 1
            if MIN_D <= d <= MAX_D:
                dates_window.add(d)
        jc = (r.get("jobCode") or "").upper()
        cc = (r.get("costCode") if isinstance(r.get("costCode"), str) else r.get("costCodeCode") or "")
        key = (jc, (cc or "").upper())
        if key in WATCH_KEYS:
            watch_hits[key] += 1
            if d:
                watch_dates.add(d)

    covered = sorted(KNOWN_STALE & dates_window)
    missed = sorted(KNOWN_STALE - dates_window)
    print(f"  rows={len(rows)} capped={capped} pages={page}")
    print(f"  distinct dates in window: {len(dates_window)}")
    print(f"  KNOWN stale covered={covered} missed={missed}")
    print(f"  watchlist keys hit: {len(watch_hits)}/{len(WATCH_KEYS)}")
    print(f"  watchlist txn dates seen: {sorted(watch_dates)}")
    return {
        "hours": hours,
        "modified_since": ms_str,
        "rows": len(rows),
        "capped": capped,
        "pages": page,
        "distinct_dates_in_window": sorted(dates_window),
        "known_stale_covered": covered,
        "known_stale_missed": missed,
        "watchlist_keys_hit": len(watch_hits),
        "watchlist_txn_dates": sorted(watch_dates),
        "top_dates_all": dates_all.most_common(20),
    }


def main() -> None:
    client = HCSSClient()
    out = {"run_at": datetime.now(timezone.utc).isoformat(), "windows": []}
    for hours, cap in [(3, 200), (24, 400), (48, 600)]:
        try:
            out["windows"].append(probe(client, hours, cap))
        except Exception as exc:
            print(f"  window {hours}h failed: {exc}")
            out["windows"].append({"hours": hours, "error": str(exc)})
    p = _REPO / "reports" / f"jobcosts_window_probe_{datetime.now():%Y%m%d_%H%M%S}.json"
    p.parent.mkdir(exist_ok=True)
    p.write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")
    print(f"\nReport: {p}")


if __name__ == "__main__":
    main()
