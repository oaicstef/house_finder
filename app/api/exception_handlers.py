"""Handlers globales de excepción.

Traducen excepciones de dominio y de validación a un cuerpo de error uniforme
(``{"error": {"code", "message", "request_id"}}``). El stack trace nunca se filtra
al cliente: los errores inesperados se registran con ``logger.exception``.
"""

from __future__ import annotations

from typing import Any

import structlog
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from starlette.responses import JSONResponse

from app.domain.exceptions import DomainException, ValidationError
from app.infrastructure.logging_config import get_logger

__all__ = ["register_exception_handlers"]

_INTERNAL_ERROR_MESSAGE = "Error interno del servidor. Contacta con soporte si persiste."


def _current_request_id() -> str | None:
    return structlog.contextvars.get_contextvars().get("request_id")


def _error_response(
    *,
    status_code: int,
    code: str,
    message: str,
    details: dict[str, Any] | None = None,
) -> JSONResponse:
    body: dict[str, Any] = {
        "error": {
            "code": code,
            "message": message,
            "request_id": _current_request_id(),
        }
    }
    if details:
        # Información adicional útil para depurar sin exponer trazas.
        body["error"]["details"] = details
    return JSONResponse(status_code=status_code, content=body)


def register_exception_handlers(app: FastAPI) -> None:
    """Registra los tres niveles de captura: dominio, validación e inesperado."""
    logger = get_logger("app.api.errors")

    async def handle_domain_exception(request: Request, exc: Exception) -> JSONResponse:
        assert isinstance(exc, DomainException)  # garantizado por add_exception_handler
        logger.warning(
            "domain_exception",
            error_code=exc.code,
            path=request.url.path,
            details=exc.details,
        )
        return _error_response(
            status_code=exc.http_status,
            code=exc.code,
            message=exc.message,
            details=exc.details,
        )

    async def handle_request_validation_error(
        request: Request,
        exc: Exception,
    ) -> JSONResponse:
        assert isinstance(exc, RequestValidationError)
        details = {
            "fields": [
                {"loc": [str(part) for part in error.get("loc", ())], "msg": error.get("msg", "")}
                for error in exc.errors()
            ]
        }
        logger.warning(
            "request_validation_failed",
            path=request.url.path,
            error_code=ValidationError.code,
            details=details,
        )
        return _error_response(
            status_code=ValidationError.http_status,
            code=ValidationError.code,
            message="La petición no es válida",
            details=details,
        )

    async def handle_unexpected_exception(request: Request, exc: Exception) -> JSONResponse:
        logger.exception(
            "unhandled_exception",
            path=request.url.path,
            error_code="INTERNAL_ERROR",
            error_type=type(exc).__name__,
        )
        return _error_response(
            status_code=500,
            code="INTERNAL_ERROR",
            message=_INTERNAL_ERROR_MESSAGE,
        )

    app.add_exception_handler(DomainException, handle_domain_exception)
    app.add_exception_handler(RequestValidationError, handle_request_validation_error)
    app.add_exception_handler(Exception, handle_unexpected_exception)
