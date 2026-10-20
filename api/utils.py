"""
Shared utility helpers used across API routes.
"""
from __future__ import annotations

import logging
from datetime import date as date_type, timedelta

from fastapi import HTTPException, status

logger = logging.getLogger(__name__)


def default_date() -> str:
    """Return yesterday's date as YYYY-MM-DD."""
    return (date_type.today() - timedelta(days=1)).strftime("%Y-%m-%d")


def validate_date(date_str: str) -> str:
    """Raise 422 if the date string is not a valid YYYY-MM-DD."""
    try:
        date_type.fromisoformat(date_str)
    except ValueError:
        logger.warning("Invalid date string received: %s", date_str)
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid date '{date_str}'. Expected format: YYYY-MM-DD.",
        )
    return date_str


def date_range(start: str, end: str) -> list[str]:
    """
    Return an inclusive list of YYYY-MM-DD strings from *start* to *end*.
    Raises 422 if either date is invalid or start > end.
    """
    start_d = date_type.fromisoformat(validate_date(start))
    end_d   = date_type.fromisoformat(validate_date(end))
    if start_d > end_d:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"date_from ({start}) must not be after date_to ({end}).",
        )
    days: list[str] = []
    current = start_d
    while current <= end_d:
        days.append(current.strftime("%Y-%m-%d"))
        current += timedelta(days=1)
    return days
