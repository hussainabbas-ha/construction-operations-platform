"""
get_jobs.py — fetch all HCSS jobs and save to JSON.
Response saved to: Test_Api/api_responses/get_jobs.json
"""
import requests
from hcss_auth import get_token
from response_saver import save_response

token = get_token()

response = requests.get(
    "https://api.hcssapps.com/heavyjob/api/v1/jobs",
    headers={
        "accept": "application/json",
        "Authorization": f"Bearer {token}",
    },
    timeout=30,
)

print(f"Status: {response.status_code}")
save_response("get_jobs", response)