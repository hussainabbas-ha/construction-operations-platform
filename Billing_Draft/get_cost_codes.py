"""
get_cost_codes.py — fetch cost codes for a job and save to JSON.
Response saved to: Test_Api/api_responses/get_cost_codes.json

Set HCSS_JOB_ID in .env to target a specific job, or edit job_ids below.
"""
import os
import requests
from dotenv import load_dotenv
from hcss_auth import get_token
from response_saver import save_response

load_dotenv()

token = get_token()
job_id = os.getenv("HCSS_JOB_ID", "4e94268c-5630-4b7a-bba6-8000efbb65e1")

response = requests.post(
    "https://api.hcssapps.com/heavyjob/api/v2/costCodes/advancedRequest",
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
save_response("get_cost_codes", response)