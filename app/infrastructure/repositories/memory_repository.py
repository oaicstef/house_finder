"""Repositorio en memoria.

Implementación de referencia del puerto ``ListingRepository``: suficiente para esta
iteración y para los tests. Al ser un adaptador de infraestructura, traduce
cualquier fallo inesperado de almacenamiento a ``RepositoryException``.
"""

from __future__ import annotations

import asyncio
from datetime import date
from typing import Any
from uuid import UUID

from app.domain.exceptions import RepositoryException
from app.domain.models import SearchResult
from app.infrastructure.logging_config import get_logger

__all__ = ["InMemoryListingRepository"]


class InMemoryListingRepository:
    """Almacén en proceso, seguro para uso concurrente en un único event loop."""

    def __init__(self, logger: Any | None = None) -> None:
        self._results: dict[UUID, SearchResult] = {}
        self._daily_index: dict[tuple[str, date], UUID] = {}
        self._lock = asyncio.Lock()
        self._logger = logger or get_logger("app.repository")

    async def save(self, result: SearchResult) -> None:
        try:
            async with self._lock:
                self._results[result.search_id] = result
                # El primer resultado del día es el que manda para la idempotencia.
                self._daily_index.setdefault(
                    (result.criteria_fingerprint, result.run_date),
                    result.search_id,
                )
        except Exception as exc:  # frontera de persistencia: se traduce a dominio
            raise RepositoryException(
                "No se ha podido guardar el resultado de la búsqueda",
                details={"search_id": str(result.search_id)},
            ) from exc

        self._logger.info(
            "search_result_persisted",
            search_id=str(result.search_id),
            matched_count=result.matched_count,
        )

    async def get_by_id(self, search_id: UUID) -> SearchResult | None:
        try:
            async with self._lock:
                return self._results.get(search_id)
        except Exception as exc:
            raise RepositoryException(
                "No se ha podido recuperar el resultado de la búsqueda",
                details={"search_id": str(search_id)},
            ) from exc

    async def find_by_criteria_and_day(
        self,
        criteria_fingerprint: str,
        run_date: date,
    ) -> SearchResult | None:
        try:
            async with self._lock:
                search_id = self._daily_index.get((criteria_fingerprint, run_date))
                return self._results.get(search_id) if search_id else None
        except Exception as exc:
            raise RepositoryException(
                "No se ha podido comprobar la idempotencia de la búsqueda diaria",
                details={"criteria_fingerprint": criteria_fingerprint},
            ) from exc

    async def clear(self) -> None:
        """Vacía el almacén (utilidad para tests)."""
        async with self._lock:
            self._results.clear()
            self._daily_index.clear()

    async def find_latest(self) -> SearchResult | None:
        """Búsqueda más reciente por ``created_at``."""
        try:
            async with self._lock:
                if not self._results:
                    return None
                return max(self._results.values(), key=lambda result: result.created_at)
        except Exception as exc:
            raise RepositoryException(
                "No se ha podido recuperar la última búsqueda",
                details={"storage": "memory"},
            ) from exc
