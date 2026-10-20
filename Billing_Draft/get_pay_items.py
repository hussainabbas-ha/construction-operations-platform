import os
import sys
import requests
from dotenv import load_dotenv

load_dotenv()

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from hcss_auth import get_token
from response_saver import save_response

token = get_token()
job_id = "4e94268c-5630-4b7a-bba6-8000efbb65e1"

url = f"https://api.hcssapps.com/heavyjob/api/v1/payItems?jobId={job_id}"

headers = {
    "accept": "application/json",
    "authorization": f"Bearer {token}",
}

response = requests.get(url, headers=headers, timeout=30)

print("Status:", response.status_code)
save_response("pay_items", response)