"""
time_cards.py — fetch timecard info and save to JSON.
Response saved to: Test_Api/api_responses/time_cards.json
"""
import requests
from hcss_auth import get_token
from response_saver import save_response

token = get_token()

response = requests.get(
    "https://api.hcssapps.com/heavyjob/api/v1/timeCardInfo",
    headers={
        "accept": "application/json",
        "Authorization": f"Bearer {token}",
    },
    timeout=30,
)

print(f"Status: {response.status_code}")
save_response("time_cards", response)