"""
Centralised logging configuration for the Construction Payroll Verification API.

Outputs structured JSON in production (LOG_FORMAT=json) and
human-readable coloured text in development (default).
"""
from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timezone


class _JsonFormatter(logging.Formatter):
    """Emit each log record as a single JSON line."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        # Attach any extra fields passed via `extra={}`
        for key, val in record.__dict__.items():
            if key not in (
                "msg", "args", "levelname", "levelno", "pathname", "filename",
                "module", "exc_info", "exc_text", "stack_info", "lineno",
                "funcName", "created", "msecs", "relativeCreated", "thread",
                "threadName", "processName", "process", "name", "message",
            ):
                payload[key] = val
        return json.dumps(payload, default=str)


class _DevFormatter(logging.Formatter):
    COLOURS = {
        "DEBUG":    "\033[36m",   # cyan
        "INFO":     "\033[32m",   # green
        "WARNING":  "\033[33m",   # yellow
        "ERROR":    "\033[31m",   # red
        "CRITICAL": "\033[35m",   # magenta
    }
    RESET = "\033[0m"

    def format(self, record: logging.LogRecord) -> str:
        colour = self.COLOURS.get(record.levelname, "")
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        base = f"{colour}{ts} [{record.levelname:<8}] {record.name}: {record.getMessage()}{self.RESET}"
        if record.exc_info:
            base += "\n" + self.formatException(record.exc_info)
        return base


def configure_logging() -> None:
    """Call once at application startup."""
    log_level = os.getenv("LOG_LEVEL", "INFO").upper()
    log_format = os.getenv("LOG_FORMAT", "text").lower()  # "json" | "text"

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(_JsonFormatter() if log_format == "json" else _DevFormatter())

    root = logging.getLogger()
    root.setLevel(log_level)
    root.handlers.clear()
    root.addHandler(handler)

    # Quieten noisy third-party loggers
    for noisy in ("uvicorn.access", "httpx", "urllib3", "requests"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
