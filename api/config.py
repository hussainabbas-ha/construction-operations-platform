"""
Application settings loaded from environment / .env file.
All config lives here — never scattered across modules.
"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

# Load .env from repo root regardless of where the process is started from
_REPO_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(_REPO_ROOT / ".env")


class Settings:
    # HCSS credentials
    hcss_client_id: str = os.getenv("HCSS_CLIENT_ID", "")
    hcss_client_secret: str = os.getenv("HCSS_CLIENT_SECRET", "")

    # API behaviour
    api_title: str = "Construction Operations API"
    api_version: str = "1.0.0"
    api_description: str = (
        "REST API for Construction Operations API (Assistant Project Manager) "
    )

    # CORS — set ALLOWED_ORIGINS=https://app.example.com,https://admin.example.com in prod
    allowed_origins: list[str] = [
        o.strip()
        for o in os.getenv("ALLOWED_ORIGINS", "*").split(",")
        if o.strip()
    ]

    # Logging
    log_level: str = os.getenv("LOG_LEVEL", "INFO").upper()
    log_format: str = os.getenv("LOG_FORMAT", "text")  # "json" in prod

    # Request tracing header name (set by load balancer / API gateway)
    request_id_header: str = "X-Request-ID"

    @property
    def docs_enabled(self) -> bool:
        """Disable Swagger UI in production by setting DOCS_ENABLED=false."""
        return os.getenv("DOCS_ENABLED", "true").lower() != "false"


settings = Settings()
