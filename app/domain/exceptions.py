"""Jerarquía de excepciones de dominio.

Cada excepción transporta un ``code`` legible por máquina (para el frontend) y el
``http_status`` con el que la capa API la traduce. El dominio no conoce FastAPI:
solo declara el código HTTP que le corresponde.
"""

from __future__ import annotations

from typing import Any, Mapping

__all__ = [
    "ConfigurationException",
    "ClassifierException",
    "DomainException",
    "GeoLocationException",
    "RepositoryException",
    "ScraperException",
    "SearchNotFoundError",
    "ValidationError",
]


class DomainException(Exception):
    """Base de todos los errores de negocio esperados."""

    code: str = "DOMAIN_ERROR"
    http_status: int = 400
    default_message: str = "Error de dominio"

    def __init__(
        self,
        message: str | None = None,
        *,
        code: str | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        self.message = message or self.default_message
        if code is not None:
            self.code = code
        self.details: dict[str, Any] = dict(details or {})
        super().__init__(self.message)

    def to_dict(self) -> dict[str, Any]:
        """Representación serializable (sin datos sensibles)."""
        return {"code": self.code, "message": self.message, "details": self.details}

    def __repr__(self) -> str:  # pragma: no cover - ayuda de depuración
        return f"{type(self).__name__}(code={self.code!r}, message={self.message!r})"


class ValidationError(DomainException):
    """Criterios de búsqueda incoherentes o incompletos."""

    code = "VALIDATION_ERROR"
    http_status = 422
    default_message = "Los criterios de búsqueda no son válidos"


class SearchNotFoundError(DomainException):
    """No existe una búsqueda con el identificador solicitado."""

    code = "SEARCH_NOT_FOUND"
    http_status = 404
    default_message = "Búsqueda no encontrada"


class ScraperException(DomainException):
    """Fallo del proveedor de scraping (Apify o el portal de origen)."""

    code = "SCRAPER_FAILURE"
    http_status = 502
    default_message = "No se han podido obtener anuncios del portal inmobiliario"


class ClassifierException(DomainException):
    """Fallo del clasificador LLM o respuesta no procesable."""

    code = "CLASSIFIER_FAILURE"
    http_status = 503
    default_message = "No se han podido clasificar los anuncios"


class ConfigurationException(DomainException):
    """Configuración de la aplicación ausente o inválida."""

    code = "CONFIGURATION_ERROR"
    http_status = 500
    default_message = "Configuración de la aplicación inválida"


class GeoLocationException(DomainException):
    """Fallo del proveedor de geocodificación (resolución de códigos postales)."""

    code = "GEO_LOCATION_FAILURE"
    http_status = 502
    default_message = "No se ha podido resolver la ubicación solicitada"


class RepositoryException(DomainException):
    """Fallo al persistir o recuperar resultados."""

    code = "REPOSITORY_FAILURE"
    http_status = 500
    default_message = "No se han podido almacenar los resultados de la búsqueda"
