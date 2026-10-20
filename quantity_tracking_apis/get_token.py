import os
import requests
from dotenv import load_dotenv

load_dotenv()

url = "https://api.hcssapps.com/identity/connect/token"

data = {
    "client_id": os.getenv("HCSS_CLIENT_ID"),
    "client_secret": os.getenv("HCSS_CLIENT_SECRET"),
    "grant_type": "client_credentials",
    "scope": os.getenv("HCSS_SCOPE", "heavyjob:read"),
}

headers = {
    "Content-Type": "application/x-www-form-urlencoded",
    "Accept": "application/json",
}

response = requests.post(url, data=data, headers=headers, timeout=30)

print("Status Code:", response.status_code)
print("Final URL:", response.url)
print("Content-Type:", response.headers.get("Content-Type"))
print("Response Text:", response.text)