"""
Request/response middleware:
  - Attaches a unique request-id to every request (reads from header or generates one)
  - Logs method, path, status code, and duration for every request
  - Stores request-id in a context var so any logger can include it
"""
from __future__ import annotations

import time
import uuid
from contextvars import ContextVar

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from api.logger import get_logger

logger = get_logger("api.access")

# Context variable — available anywhere in the same async task
request_id_ctx: ContextVar[str] = ContextVar("request_id", default="-")


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:  # type: ignore[override]
        req_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
        request_id_ctx.set(req_id)

        start = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            logger.exception(
                "Unhandled exception",
                extra={"request_id": req_id, "path": request.url.path},
            )
            raise
        finally:
            duration_ms = round((time.perf_counter() - start) * 1000, 1)

        logger.info(
            "%s %s → %s  (%.1f ms)",
            request.method,
            request.url.path,
            response.status_code,
            duration_ms,
            extra={
                "request_id": req_id,
                "method": request.method,
                "path": request.url.path,
                "status_code": response.status_code,
                "duration_ms": duration_ms,
            },
        )

        response.headers["X-Request-ID"] = req_id
        return response
