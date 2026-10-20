"""
Shared JSON response saver for all Test_Api scripts.

Usage:
    from response_saver import save_response

    save_response("get_jobs", response)          # saves api_responses/get_jobs.json
    save_response("get_business_units", response) # saves api_responses/get_business_units.json
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import requests

# All JSON files are saved here, next to this file
_OUTPUT_DIR = Path(__file__).parent / "api_responses"


def save_response(name: str, response: requests.Response) -> Path:
    """
    Save an HTTP response to  Test_Api/api_responses/<name>.json  in clean format.

    Works for both:
      - Successful responses  (status 2xx) → saves parsed JSON
      - Error responses       (status 4xx/5xx) → saves status + raw text so
        you still have something to inspect

    Returns the path of the file that was written.
    """
    _OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = _OUTPUT_DIR / f"{name}.json"

    # Try to parse body as JSON; fall back to raw text
    try:
        body = response.json()
    except Exception:
        body = response.text

    payload = {
        "status_code": response.status_code,
        "url": str(response.url),
        "response": body,
    }

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)

    size_kb = out_path.stat().st_size / 1024
    print(f"[saver] Saved → {out_path}  ({size_kb:.1f} KB)")
    return out_path