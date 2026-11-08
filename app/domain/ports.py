"""Puertos (interfaces) del hexágono.

Los servicios de aplicación dependen exclusivamente de estos contratos (DIP); las
implementaciones concretas viven en ``app/infrastructure``. Se usa ``Protocol``
para tipado estructural: una implementación no necesita heredar de nada.
"""

from __future__ import annotations

from datetime import date
from typing import Protocol, Sequence, runtime_checkable
from uuid import UUID

from app.domain.models import (
    Coordinates,
    Listing,
    ListingAssessment,
    SearchCriteria,
    SearchResult,
)

__all__ = [
    "ClassifierPort",
    "ListingRepository",
    "PostalCodeLocatorPort",
    "ScraperPort",
    "ScheduledSearchServiceProtocol",
    "SearchProfileStore",
    "SearchServiceProtocol",
]


@runtime_checkable
class ScraperPort(Protocol):
    """Obtiene anuncios normalizados de uno o varios portales."""

    async def fetch_listings(self, criteria: SearchCriteria) -> list[Listing]:
        """Devuelve anuncios que cumplen los criterios duros.

        Raises:
            ScraperException: si el proveedor de scraping falla.
        """
        ...


@runtime_checkable
class PostalCodeLocatorPort(Protocol):
    """Resuelve un código postal español en un punto geográfico."""

    async def locate(self, postal_code: str) -> Coordinates | None:
        """Devuelve ``None`` si el CP no existe.

        Raises:
            GeoLocationException: si el proveedor de geocodificación falla.
        """
        ...


@runtime_checkable
class ClassifierPort(Protocol):
    """Puntúa anuncios contra los criterios del usuario."""

    async def assess_listings(
        self,
        criteria: SearchCriteria,
        listings: Sequence[Listing],
    ) -> list[ListingAssessment]:
        """Devuelve una evaluación por anuncio.

        Raises:
            ClassifierException: si el modelo no responde o la respuesta es inválida.
        """
        ...


@runtime_checkable
class ListingRepository(Protocol):
    """Persistencia de resultados de búsqueda."""

    async def save(self, result: SearchResult) -> None:
        """Guarda la ejecución completa (idempotente por ``search_id``)."""
        ...

    async def get_by_id(self, search_id: UUID) -> SearchResult | None:
        """Devuelve ``None`` si no existe; el servicio traduce a 404."""
        ...

    async def find_by_criteria_and_day(
        self,
        criteria_fingerprint: str,
        run_date: date,
    ) -> SearchResult | None:
        """Soporte de idempotencia del job diario: ``None`` si no hay ejecución."""
        ...

    async def find_latest(self) -> SearchResult | None:
        """Búsqueda más reciente guardada; ``None`` si no hay ninguna."""
        ...


@runtime_checkable
class SearchProfileStore(Protocol):
    """Persistencia del perfil de búsqueda editado por el usuario.

    El perfil guardado tiene prioridad sobre ``SEARCH_PROFILE_JSON``; si no existe,
    el servicio usa el perfil de configuración.
    """

    async def get(self) -> SearchCriteria | None:
        """Devuelve ``None`` si no hay perfil guardado o no es legible."""
        ...

    async def save(self, criteria: SearchCriteria) -> None:
        """Guarda el perfil de forma atómica.

        Raises:
            RepositoryException: si el almacenamiento falla.
        """
        ...

    async def clear(self) -> None:
        """Borra el perfil guardado (idempotente)."""
        ...


class SearchServiceProtocol(Protocol):
    """Caso de uso: ejecutar y consultar búsquedas."""

    async def run_search(self, criteria: SearchCriteria, *, trigger: str = "api") -> SearchResult:
        ...

    async def get_results(self, search_id: UUID) -> SearchResult:
        ...

    async def get_latest(self) -> SearchResult:
        ...


class ScheduledSearchServiceProtocol(Protocol):
    """Caso de uso: búsqueda diaria programada."""

    async def run_daily_job(self, *, raise_on_error: bool = True) -> SearchResult | None:
        ...

    async def scheduler_entrypoint(self) -> None:
        ...
