"""
Show raw progress API rows for mismatched cost codes in job 25048.
Pass date range as args: python probe_raw_progress.py 2026-06-01 2026-06-15
"""
import sys
import json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dotenv import load_dotenv
load_dotenv()
from payroll_verification.hcss_client import HCSSClient

start = sys.argv[1] if len(sys.argv) > 1 else "2026-06-01"
end   = sys.argv[2] if len(sys.argv) > 2 else "2026-06-15"

client = HCSSClient()
job_id = "10c62c1b-e012-4664-95b7-0a3bb1338928"
url = "https://api.hcssapps.com/heavyjob/api/v1/costCode/progress/advancedRequest"

# Cost code IDs for the mismatched ones
target_ccids = {
    "63f15b6d-77ce-4c1b-a1fe-5a8f15cfbd96",  # 14511111
    "ece6fec1-d869-4e14-977c-d8ff3c3fa335",  # 15951111
    "2a535f91-ffb1-4933-b40b-11be4f0a892e",  # 16151111A
    "27edea24-f306-4bea-a1d3-1b54fdc3af45",  # 16201111B
    "bf3671f5-5bee-4f7d-987c-fd37fbaabd07",  # 16351111B
    "66d049f5-7c33-48b5-93c8-3ef481254820",  # 17451111C
}

rows = []
cursor = None
while True:
    payload = {
        "jobId":     job_id,
        "startDate": f"{start}T00:00:00Z",
        "endDate":   f"{end}T23:59:59Z",
        "limit":     500,
    }
    if cursor:
        payload["cursor"] = cursor
    raw = client._request("POST", url, json=payload)
    batch, next_cursor = client._extract_results_and_cursor(raw)
    rows.extend(batch)
    if not next_cursor:
        break
    cursor = next_cursor

print(f"Total progress rows returned: {len(rows)}")
if rows:
    print(f"Sample row keys: {list(rows[0].keys())}")
    print()

# Show all fields for mismatched cost codes
print("=" * 80)
print(f"RAW ROWS for mismatched cost codes  ({start} -> {end})")
print("=" * 80)
for row in rows:
    ccid = row.get("costCodeId") or row.get("id") or ""
    if ccid in target_ccids:
        print(json.dumps(row, indent=2, default=str))
        print()
