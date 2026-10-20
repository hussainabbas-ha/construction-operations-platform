"""
Shared HCSS authentication helper for all Test_Api scripts.

PERMANENT TOKEN SOLUTION:
  - Never reads HCSS_ACCESS_TOKEN from .env (that token expires in 1 hour).
  - Always fetches a fresh token using client_credentials grant with
    HCSS_CLIENT_ID + HCSS_CLIENT_SECRET (these never expire).
  - Caches the token in memory for the lifetime of the process so multiple
    API calls in the same script only fetch one token.

Usage in any test script:
    from hcss_auth import get_token
    token = get_token()
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta

import requests
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# In-process token cache  (reset each time the script is run — that's fine)
# ---------------------------------------------------------------------------
_cached_token: str | None = None
_token_expiry: datetime | None = None


def get_token() -> str:
    """
    Return a valid HCSS bearer token, fetching a new one only when needed.
    Uses HCSS_CLIENT_ID + HCSS_CLIENT_SECRET from .env — these never expire.
    """
    global _cached_token, _token_expiry

    # Return cached token if still valid (with 60-second safety margin)
    if _cached_token and _token_expiry and datetime.now() < _token_expiry:
        print(f"[auth] Using cached token (expires {_token_expiry.strftime('%H:%M:%S')})")
        return _cached_token

    client_id = os.getenv("HCSS_CLIENT_ID", "").strip()
    client_secret = os.getenv("HCSS_CLIENT_SECRET", "").strip()

    if not client_id or not client_secret:
        raise RuntimeError(
            "HCSS_CLIENT_ID and HCSS_CLIENT_SECRET must be set in .env\n"
            "Do NOT use HCSS_ACCESS_TOKEN — it expires after 1 hour."
        )

    print("[auth] Fetching new HCSS token via client_credentials …")

    response = requests.post(
        "https://api.hcssapps.com/identity/connect/token",
        data={
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
            "scope": os.getenv("HCSS_SCOPE", "heavyjob:read timecards:read"),
        },
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        },
        timeout=30,
    )

    response.raise_for_status()
    data = response.json()

    _cached_token = data["access_token"]
    expires_in = int(data.get("expires_in", 3600))
    _token_expiry = datetime.now() + timedelta(seconds=expires_in - 60)

    print(f"[auth] Token fetched successfully (valid for {expires_in}s, "
          f"expires at {_token_expiry.strftime('%H:%M:%S')})")
    return _cached_token