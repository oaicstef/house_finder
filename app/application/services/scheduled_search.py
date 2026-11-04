"""Caso de uso: búsqueda diaria programada e idempotente.

El job se registra con APScheduler en el ``lifespan`` de FastAPI. Toda excepción se
captura dentro del propio job: una tarea programada que lanza pierde el error y no
deja rastro en los handlers HTTP.
"""

from __future__ import annotations

import time
from datetime import date
from typing import Any, Callable

from app.domain.exceptions import DomainException
from app.domain.models import SearchCriteria, SearchResult
from app.domain.ports import ListingRepository, SearchServiceProtocol
from app.infrastructure.logging_config import get_logger

__all__ = ["ScheduledSearchService", "SCHEDULED_JOB_ID"]

#: Identificador del job en APScheduler.
SCHEDULED_JOB_ID = "daily-real-estate-search"


class ScheduledSearchService:
    """Ejecuta la búsqueda diaria garantizando como máximo una ejecución por día."""

    def __init__(
        self,
        search_service: SearchServiceProtocol,
        repository: ListingRepository,
        criteria: SearchCriteria,
        *,
        logger: Any | None = None,
        today: Callable[[], date] | None = None,
    ) -> None:
        self._search_service = search_service
        self._repository = repository
        self._criteria = criteria
        self._logger = logger or get_logger("app.scheduler")
        self._today = today or date.today

    async def run_daily_job(self, *, raise_on_error: bool = True) -> SearchResult | None:
        """Ejecuta la búsqueda del día.

        Returns:
            El resultado creado, o ``None`` si ya existía una ejecución para el
            mismo criterio y fecha (idempotencia).

        Raises:
            DomainException: solo si ``raise_on_error`` es ``True`` (invocación manual).
        """
        run_date = self._today()
        fingerprint = self._criteria.fingerprint()
        started = time.perf_counter()

        previous = await self._repository.find_by_criteria_and_day(fingerprint, run_date)
        if previous is not None:
            self._logger.warning(
                "scheduled_search_skipped_duplicate",
                search_id=str(previous.search_id),
                criteria_fingerprint=fingerprint,
                run_date=run_date.isoformat(),
                duration_ms=self._elapsed_ms(started),
            )
            return None

        try:
            result = await self._search_service.run_search(self._criteria, trigger="scheduler")
        except DomainException as exc:
            self._logger.warning(
                "scheduled_search_failed",
                error_code=exc.code,
                criteria_fingerprint=fingerprint,
                run_date=run_date.isoformat(),
                duration_ms=self._elapsed_ms(started),
            )
            if raise_on_error:
                raise
            return None
        except Exception:  # red de seguridad: el job nunca debe propagar
            self._logger.exception(
                "scheduled_search_unexpected_failure",
                error_code="INTERNAL_ERROR",
                criteria_fingerprint=fingerprint,
                run_date=run_date.isoformat(),
                duration_ms=self._elapsed_ms(started),
            )
            if raise_on_error:
                raise
            return None

        self._logger.info(
            "scheduled_search_completed",
            search_id=str(result.search_id),
            matched_count=result.matched_count,
            criteria_fingerprint=fingerprint,
            run_date=run_date.isoformat(),
            duration_ms=self._elapsed_ms(started),
        )
        return result

    async def scheduler_entrypoint(self) -> None:
        """Callable registrado en APScheduler: nunca propaga excepciones."""
        try:
            await self.run_daily_job(raise_on_error=False)
        except Exception:  # defensa extra ante fallos del repositorio previo
            self._logger.exception(
                "scheduled_search_entrypoint_failure",
                error_code="INTERNAL_ERROR",
            )

    @staticmethod
    def _elapsed_ms(started: float) -> float:
        return round((time.perf_counter() - started) * 1000, 2)
