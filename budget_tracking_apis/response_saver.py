"""
Shared JSON response saver for budget tracking API scripts.

Usage:
    from response_saver import save_response

    save_response("get_budget_summary", data)  # saves api_responses/get_budget_summary.json
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

# All JSON files are saved here, next to this file
_OUTPUT_DIR = Path(__file__).parent / "api_responses"


def save_response(name: str, data: Any) -> Path:
    """
    Save API response data to budget_tracking_apis/api_responses/<name>.json in clean format.

    Args:
        name: Base filename (without .json extension)
        data: Data to save (will be JSON serialized)

    Returns:
        Path to the saved file
    """
    _OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = _OUTPUT_DIR / f"{name}.json"

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

    size_kb = out_path.stat().st_size / 1024
    print(f"[saver] Saved → {out_path}  ({size_kb:.1f} KB)")
    return out_path
