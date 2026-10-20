"""
Probe payItemFactor for mismatched cost codes in job 25048.
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dotenv import load_dotenv
load_dotenv()
from payroll_verification.hcss_client import HCSSClient

client = HCSSClient()
job_id = "10c62c1b-e012-4664-95b7-0a3bb1338928"
url = "https://api.hcssapps.com/heavyjob/api/v1/costCodes"

rows = []
cursor = None
while True:
    params = {"jobId": job_id, "limit": 500}
    if cursor:
        params["cursor"] = cursor
    raw = client._request("GET", url, params=params)
    batch, next_cursor = client._extract_results_and_cursor(raw)
    rows.extend(batch)
    if not next_cursor:
        break
    cursor = next_cursor

target = {"14511111", "15951111", "16151111A", "16201111B", "16351111B", "17451111C"}
print(f"{'Code':<16} {'qty':>10} {'payItemFactor':>14} {'unit':<8} {'isPayItemDriver'}")
print("-" * 65)
for r in rows:
    if r.get("code") in target:
        print(
            f"{r.get('code',''):<16} "
            f"{r.get('quantity', 0):>10} "
            f"{r.get('payItemFactor', 'N/A'):>14} "
            f"{r.get('unitOfMeasure',''):<8} "
            f"{r.get('isPayItemDriver', '')}"
        )
