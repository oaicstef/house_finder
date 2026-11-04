"""Configuración de logging estructurado con structlog.

- Producción: ``JSONRenderer`` (una línea JSON por evento, apto para agregadores).
- Desarrollo: ``ConsoleRenderer`` legible por humanos.
- Todo evento incluye los campos obligatorios del proyecto gracias al procesador
  ``ensure_required_fields``, que los rellena con ``None`` si no aplican.
"""

from __future__ import annotations

import logging
from typing import Any

import structlog

from app.config import Settings, get_settings

#: Campos presentes en todos los eventos, según la especificación del feature.
REQUIRED_LOG_FIELDS: tuple[str, ...] = (
    "event",
    "request_id",
    "search_id",
    "listing_url",
    "duration_ms",
    "error_code",
)

_configured = False


def ensure_required_fields(
    logger: Any,
    method_name: str,
    event_dict: dict[str, Any],
) -> dict[str, Any]:
    """Procesador que garantiza la presencia de los campos obligatorios."""
    for field_name in REQUIRED_LOG_FIELDS:
        event_dict.setdefault(field_name, None)
    return event_dict


def build_processors(*, json_output: bool) -> list[Any]:
    """Cadena de procesadores de structlog según el entorno."""
    processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        ensure_required_fields,
    ]
    if json_output:
        processors.append(structlog.processors.JSONRenderer(ensure_ascii=False))
    else:
        processors.append(structlog.dev.ConsoleRenderer(colors=True))
    return processors


def configure_logging(settings: Settings | None = None, *, force: bool = False) -> None:
    """Idempotente: reconfigura structlog y el logging estándar."""
    global _configured
    if _configured and not force:
        return

    resolved = settings or get_settings()
    level = getattr(logging, resolved.log_level, logging.INFO)

    logging.basicConfig(
        format="%(message)s",
        level=level,
        handlers=[logging.StreamHandler()],
        force=True,
    )
    # Silencia el ruido de librerías de terceros en niveles altos.
    for noisy_logger in ("httpx", "httpcore", "apscheduler", "openai"):
        logging.getLogger(noisy_logger).setLevel(max(level, logging.WARNING))

    structlog.configure(
        processors=build_processors(json_output=resolved.json_logs),
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )
    _configured = True


def get_logger(name: str = "app") -> Any:
    """Logger de structlog listo para usar con ``**kwargs`` de contexto."""
    return structlog.get_logger(name)
