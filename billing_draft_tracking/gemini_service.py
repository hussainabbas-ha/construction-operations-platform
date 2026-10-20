"""
Gemini (Vertex AI) service for the Billing Draft module.

Uses the service-account in vertex.json against project `neurainternalnoorg`,
location `global`, model `gemini-3.1-flash-lite` (all overridable via env).

Only the LLM *judgment* (line-item classification + notes) goes through here;
all dollar math is done in Python (see aia_generator) so financial totals are
never an LLM arithmetic guess.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[1]

_PROJECT = os.getenv("BILLING_AI_PROJECT", "neurainternalnoorg")
_LOCATION = os.getenv("BILLING_AI_LOCATION", "global")
_MODEL = os.getenv("BILLING_AI_MODEL", "gemini-3.1-flash-lite")
_CREDS_PATH = os.getenv("BILLING_AI_CREDENTIALS") or os.getenv(
    "VERTEX_CREDENTIALS_PATH", str(_REPO_ROOT / "vertex.json")
)

_client = None  # lazily created, reused across calls


def _get_client():
    global _client
    if _client is not None:
        return _client
    from google import genai
    from google.oauth2 import service_account

    creds_path = _CREDS_PATH if os.path.isabs(_CREDS_PATH) else str(_REPO_ROOT / _CREDS_PATH)
    creds = service_account.Credentials.from_service_account_file(
        creds_path, scopes=["https://www.googleapis.com/auth/cloud-platform"]
    )
    _client = genai.Client(vertexai=True, project=_PROJECT, location=_LOCATION, credentials=creds)
    logger.info("Gemini client ready (project=%s location=%s model=%s)", _PROJECT, _LOCATION, _MODEL)
    return _client


def generate_json(system_instruction: str, prompt: str, *, temperature: float = 0.1) -> dict[str, Any]:
    """Call Gemini and parse a JSON object response. Raises on transport/parse error."""
    from google.genai import types

    client = _get_client()
    resp = client.models.generate_content(
        model=_MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(
            system_instruction=system_instruction,
            temperature=temperature,
            response_mime_type="application/json",
        ),
    )
    text = (resp.text or "").strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Defensive: strip ``` fences if the model wrapped the JSON
        cleaned = text.strip().lstrip("`").strip()
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:].strip()
        cleaned = cleaned.rstrip("`").strip()
        return json.loads(cleaned)


def model_info() -> dict[str, str]:
    return {"project": _PROJECT, "location": _LOCATION, "model": _MODEL}
