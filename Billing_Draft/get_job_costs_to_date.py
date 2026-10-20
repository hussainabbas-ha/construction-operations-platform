"""
get_job_costs_to_date.py — fetch job costs to date and save to JSON.
Response saved to: Test_Api/api_responses/get_job_costs_to_date.json

Set HCSS_JOB_ID in .env to target a specific job.
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

response = requests.post(
    "https://api.hcssapps.com/heavyjob/api/v1/jobCostsToDate/search",
    headers={
        "accept": "application/json",
        "content-type": "application/json",
        "Authorization": f"Bearer {token}",
    },
    json={
        "jobIds": [job_id],
        "limit": 100,
    },
    timeout=30,
)

print(f"Status: {response.status_code}")
save_response("get_job_costs_to_date", response)