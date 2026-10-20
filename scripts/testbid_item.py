"""
testbid_item.py
───────────────
Fetch HeavyBid bid items for a specific business unit.

API endpoint:
  GET https://api.hcssapps.com/heavybid/api/v2/integration/businessunits/{businessUnitId}/biditems

Usage:
  python testbid_item.py
"""
from __future__ import annotations

import json
import os
import sys

import requests
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
BUSINESS_UNIT_ID = "b26b89dc-9ddd-4ef9-8a02-a92ce68af547"
BASE_URL = "https://api.hcssapps.com"

# ---------------------------------------------------------------------------
# Auth — fetch a fresh token (same pattern as Billing_Draft/hcss_auth.py)
# ---------------------------------------------------------------------------

def get_token() -> str:
    client_id = os.getenv("HCSS_CLIENT_ID", "").strip()
    client_secret = os.getenv("HCSS_CLIENT_SECRET", "").strip()

    if not client_id or not client_secret:
        print("[ERROR] HCSS_CLIENT_ID and HCSS_CLIENT_SECRET must be set in .env")
        sys.exit(1)

    print("[auth] Fetching HCSS token via client_credentials ...")

    resp = requests.post(
        f"{BASE_URL}/identity/connect/token",
        data={
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
            "scope": "heavyjob:read timecards:read",
        },
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        },
        timeout=30,
    )

    if resp.status_code != 200:
        print(f"[ERROR] Token request failed: {resp.status_code}")
        print(resp.text)
        sys.exit(1)

    token = resp.json()["access_token"]
    print("[auth] Token fetched successfully.\n")
    return token


# ---------------------------------------------------------------------------
# Fetch bid items
# ---------------------------------------------------------------------------

def fetch_bid_items(token: str, business_unit_id: str) -> None:
    url = (
        f"{BASE_URL}/heavybid/api/v2/integration/businessunits"
        f"/{business_unit_id}/biditems"
    )

    headers = {
        "accept": "application/json",
        "authorization": f"Bearer {token}",
    }

    print(f"[request] GET {url}")
    resp = requests.get(url, headers=headers, timeout=30)

    print(f"[response] Status: {resp.status_code}")

    if resp.status_code == 200:
        data = resp.json()

        # Pretty-print to console
        print(json.dumps(data, indent=2))

        # Save to file for inspection
        out_path = os.path.join(os.path.dirname(__file__), "bid_items_response.json")
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        print(f"\n[saved] Response written to {out_path}")

        # Quick summary
        items = data if isinstance(data, list) else data.get("items", data.get("data", []))
        if isinstance(items, list):
            print(f"\n[summary] {len(items)} bid item(s) returned.")

    elif resp.status_code == 401:
        print("[ERROR] 401 Unauthorized — token may not have heavybid:read scope.")
        print("        Add heavybid:read to your HCSS app's allowed scopes in the HCSS portal.")
        print(resp.text)

    elif resp.status_code == 403:
        print("[ERROR] 403 Forbidden — this client_id may not have access to HeavyBid.")
        print(resp.text)

    elif resp.status_code == 404:
        print(f"[ERROR] 404 Not Found — business unit ID '{business_unit_id}' may not exist.")
        print(resp.text)

    else:
        print(f"[ERROR] Unexpected response:")
        print(resp.text)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    token = get_token()
    fetch_bid_items(token, BUSINESS_UNIT_ID)
