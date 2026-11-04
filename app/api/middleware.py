"""Middleware de contexto HTTP.

Genera (o propaga) el ``request_id``, lo vincula al contexto de structlog para que
aparezca en todos los logs de la petición, mide la duración y expone la cabecera
``X-Request-ID`` al cliente.
"""

from __future__ import annotations

import time
from typing import Any
from uuid import uuid4

import structlog
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from app.infrastructure.logging_config import get_logger

__all__ = ["RequestContextMiddleware"]

REQUEST_ID_HEADER = "X-Request-ID"


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Aísla el contexto de logging de cada petición."""

    def __init__(self, app: Any, *, logger: Any | None = None) -> None:
        super().__init__(app)
        self._logger = logger or get_logger("app.http")

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        request_id = request.headers.get(REQUEST_ID_HEADER) or str(uuid4())
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(
            request_id=request_id,
            method=request.method,
            path=request.url.path,
        )

        started = time.perf_counter()
        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
            response.headers[REQUEST_ID_HEADER] = request_id
            return response
        finally:
            duration_ms = round((time.perf_counter() - started) * 1000, 2)
            log_method = self._logger.info if status_code < 500 else self._logger.warning
            log_method(
                "http_request_completed",
                status_code=status_code,
                duration_ms=duration_ms,
                error_code=None if status_code < 400 else f"HTTP_{status_code}",
            )
            structlog.contextvars.clear_contextvars()
