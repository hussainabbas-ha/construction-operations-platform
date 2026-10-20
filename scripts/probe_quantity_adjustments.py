"""Probe HCSS quantity adjustments API with multiple filters."""
from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))

from dotenv import load_dotenv

load_dotenv(_REPO / ".env")

from payroll_verification.hcss_client import HCSSClient

URL = "https://api.hcssapps.com/heavyjob/api/v1/costTypeQuantities/adjustments"
WATCH = {
    ("25006", "80711111"),
    ("25006", "80011111B"),
    ("25006", "58011111D"),
    ("25069C", "14511111C"),
    ("26004", "61340010A"),
    ("25060CONST", "40011111"),
    ("25060CONST", "24011111A"),
    ("25048", "12011111C"),
    ("25006", "58511111C"),
    ("25006", "5111111"),
    ("25060CONST", "211111I"),
    ("25006", "60011111B"),
    ("25006", "1011111"),
}


def fetch(client: HCSSClient, params: dict, label: str) -> list[dict]:
    print("=" * 60)
    print(label, params)
    rows: list[dict] = []
    cursor = None
    pages = 0
    while True:
        p = dict(params)
        if cursor:
            p["cursor"] = cursor
        raw = client._request("GET", URL, params=p)
        batch, nxt = client._extract_results_and_cursor(raw)
        pages += 1
        rows.extend(batch)
        print(f"  page {pages}: {len(batch)} (total {len(rows)})")
        if not nxt:
            break
        cursor = nxt
        if pages >= 30:
            print("  stop at 30 pages")
            break
    print(f"  TOTAL={len(rows)}")
    if rows:
        print("  fields:", sorted(rows[0].keys()))
        dates = Counter((str(r.get("date") or "")[:10]) for r in rows)
        mods = Counter(
            (str(r.get("lastModifiedPreciseDateTime") or "")[:10]) for r in rows
        )
        print("  top txn dates:", dates.most_common(15))
        print("  top lastModified days:", mods.most_common(15))
        jobs = Counter(r.get("jobCode") for r in rows)
        print("  top jobs:", jobs.most_common(10))
        hits = [
            r
            for r in rows
            if (
                (r.get("jobCode") or "").upper(),
                (r.get("costCode") or "").upper(),
            )
            in WATCH
        ]
        print(f"  watchlist hits: {len(hits)}")
        for r in hits[:40]:
            print(
                f"    {str(r.get('date'))[:10]} {r.get('jobCode')} "
                f"{r.get('costCode')} qty={r.get('quantity')} "
                f"mod={r.get('lastModifiedPreciseDateTime')} rev={r.get('revision')}"
            )
    dirty = sorted(
        {
            str(r.get("date") or "")[:10]
            for r in rows
            if r.get("date")
            and "2026-05-01" <= str(r.get("date"))[:10] <= "2026-07-20"
        }
    )
    out = {
        "label": label,
        "params": params,
        "count": len(rows),
        "sample": rows[:5],
        "dirty_in_window": dirty,
        "watchlist_hit_count": sum(
            1
            for r in rows
            if (
                (r.get("jobCode") or "").upper(),
                (r.get("costCode") or "").upper(),
            )
            in WATCH
        ),
    }
    if len(rows) <= 500:
        out["rows"] = rows
    reports = _REPO / "reports"
    reports.mkdir(exist_ok=True)
    safe = (
        label.replace(" ", "_")
        .replace(":", "")
        .replace("/", "-")
    )
    path = reports / f"adj_probe_{safe}.json"
    path.write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")
    print(f"  wrote {path}")
    return rows


def main() -> None:
    client = HCSSClient()
    fetch(client, {"modifiedSince": "2026-05-01T00:00:00Z", "limit": 1000}, "modifiedSince_May1")
    fetch(client, {"modifiedSince": "2026-07-01T00:00:00Z", "limit": 1000}, "modifiedSince_Jul1")
    fetch(client, {"modifiedSince": "2026-07-07T00:00:00Z", "limit": 1000}, "modifiedSince_Jul7")
    fetch(
        client,
        {"startDate": "2026-05-01", "endDate": "2026-07-20", "limit": 1000},
        "startEnd_May1_Jul20",
    )
    fetch(
        client,
        {"startDate": "2026-07-14", "endDate": "2026-07-20", "limit": 1000},
        "startEnd_last7",
    )
    print("DONE PROBES")


if __name__ == "__main__":
    main()
