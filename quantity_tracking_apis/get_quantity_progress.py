"""
get_quantity_progress.py — fetch quantity/cost-code progress for a date range and save to JSON.
Response saved to: Test_Api/api_responses/get_quantity_progress.json

Set HCSS_JOB_ID in .env, and edit start_date / end_date below as needed.
"""
import os
import requests
from dotenv import load_dotenv
from hcss_auth import get_token
from response_saver import save_response

load_dotenv()

token = get_token()
job_id = "4e94268c-5630-4b7a-bba6-8000efbb65e1"

if not job_id:
    raise ValueError("HCSS_JOB_ID is not set in .env")

# Edit these dates to match the range you want to inspect
start_date = "2026-04-01T00:00:00Z"
end_date   = "2026-04-13T23:59:59Z"

response = requests.post(
    "https://api.hcssapps.com/heavyjob/api/v1/costCode/progress/advancedRequest",
    headers={
        "accept": "application/json",
        "content-type": "application/json",
        "Authorization": f"Bearer {token}",
    },
    json={
        "jobId": job_id,
        "startDate": start_date,
        "endDate": end_date,
        "limit": 100,
    },
    timeout=30,
)

print(f"Status: {response.status_code}")
save_response("get_quantity_progress", response)