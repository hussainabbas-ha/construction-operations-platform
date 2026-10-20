"""
Understand what jobCosts?modifiedSince actually returns for this tenant.

Cap pages low (cheap), then show the transaction-date histogram of the rows
returned. If dates span the whole history -> modifiedSince reflects a broad
re-sync (bad precise-dirty signal). If concentrated recent -> genuine edits.

Also test scoping by active jobIds to see if volume drops.
"""
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
_JOBS = "https://api.hcssapps.com/heavyjob/api/v1/jobs"
KNOWN_STALE = {"2026-07-15", "2026-07-17", "2026-07-20"}


def _month(d: str) -> str:
    return d[:7] if len(d) >= 7 else "?"


def paginate(client, payload, max_pages, delay_pages=0):
    rows = []
    cursor = None
    page = 0
    while True:
        body = dict(payload)
        if cursor:
            body["cursor"] = cursor
        raw = client._request("POST", _JOB_COSTS_V1, json=body)
        batch, nxt = client._extract_results_and_cursor(raw)
        page += 1
        rows.extend(batch)
        if not nxt or page >= max_pages:
            return rows, (nxt is not None), page
        cursor = nxt


def summarize(rows):
    dates = Counter(str(r.get("date") or "")[:10] for r in rows if r.get("date"))
    months = Counter(_month(str(r.get("date") or "")[:10]) for r in rows if r.get("date"))
    et = Counter(r.get("entryType") or "" for r in rows)
    known = {d for d in dates if d in KNOWN_STALE}
    return {
        "rows": len(rows),
        "distinct_dates": len(dates),
        "min_date": min(dates) if dates else None,
        "max_date": max(dates) if dates else None,
        "by_month": dict(sorted(months.items())),
        "entry_types": dict(et),
        "known_stale_seen": sorted(known),
        "recent_dates": sorted([d for d in dates if d >= "2026-07-01"]),
    }


def main() -> None:
    client = HCSSClient()
    out: dict[str, Any] = {"run_at": datetime.now(timezone.utc).isoformat()}

    for hours in [24, 72]:
        ms = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%S")
        print(f"\n=== modifiedSince last {hours}h ({ms}), cap 30 pages ===")
        rows, more, pages = paginate(client, {"modifiedSince": ms, "limit": 500}, max_pages=30)
        s = summarize(rows)
        s["has_more_beyond_cap"] = more
        s["pages_fetched"] = pages
        s["modified_since"] = ms
        print(json.dumps(s, indent=2, default=str))
        out[f"modifiedSince_{hours}h_cap30"] = s

    p = _REPO / "reports" / f"jobcosts_datespread_{datetime.now():%Y%m%d_%H%M%S}.json"
    p.parent.mkdir(exist_ok=True)
    p.write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")
    print(f"\nReport: {p}")


if __name__ == "__main__":
    main()
