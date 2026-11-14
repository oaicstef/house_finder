"""Caso de uso: búsqueda diaria programada e idempotente.

El job se registra con APScheduler en el ``lifespan`` de FastAPI. Toda excepción se
captura dentro del propio job: una tarea programada que lanza pierde el error y no
deja rastro en los handlers HTTP.
"""

from __future__ import annotations

import time
from datetime import date
from typing import Any, Awaitable, Callable

from app.application.services.listing_alerts import build_digest, select_new_listings
from app.domain.exceptions import DomainException
from app.domain.models import SearchCriteria, SearchResult
from app.domain.ports import AlertNotifierPort, ListingRepository, SearchServiceProtocol
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
        criteria_provider: Callable[[], Awaitable[SearchCriteria]] | None = None,
        notifier: AlertNotifierPort | None = None,
        alert_click_url: str | None = None,
        logger: Any | None = None,
        today: Callable[[], date] | None = None,
    ) -> None:
        self._search_service = search_service
        self._repository = repository
        self._criteria = criteria
        # El perfil puede cambiar desde la UI: se resuelve al inicio de cada ejecución.
        self._criteria_provider = criteria_provider
        # Sin notificador configurado el job funciona igual, sin alertas.
        self._notifier = notifier
        self._alert_click_url = alert_click_url
        self._logger = logger or get_logger("app.scheduler")
        self._today = today or date.today

    async def _resolve_criteria(self) -> SearchCriteria:
        if self._criteria_provider is None:
            return self._criteria
        return await self._criteria_provider()

    async def run_daily_job(self, *, raise_on_error: bool = True) -> SearchResult | None:
        """Ejecuta la búsqueda del día.

        Returns:
            El resultado creado, o ``None`` si ya existía una ejecución para el
            mismo criterio y fecha (idempotencia).

        Raises:
            DomainException: solo si ``raise_on_error`` es ``True`` (invocación manual).
        """
        criteria = await self._resolve_criteria()
        run_date = self._today()
        fingerprint = criteria.fingerprint()
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

        previous = await self._find_previous(run_date, fingerprint)

        try:
            result = await self._search_service.run_search(criteria, trigger="scheduler")
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
        await self._notify_new_listings(result, previous)
        return result

    async def _find_previous(
        self,
        run_date: date,
        fingerprint: str,
    ) -> SearchResult | None:
        """Ejecución programada anterior; un fallo aquí no debe tumbar la búsqueda."""
        try:
            return await self._repository.find_previous_scheduled(fingerprint, run_date)
        except Exception:  # la alerta es un extra: nunca bloquea el job
            self._logger.warning(
                "scheduled_search_previous_lookup_failed",
                error_code="REPOSITORY_FAILURE",
                criteria_fingerprint=fingerprint,
                run_date=run_date.isoformat(),
            )
            return None

    async def _notify_new_listings(
        self,
        result: SearchResult,
        previous: SearchResult | None,
    ) -> None:
        """Envía un único resumen con las viviendas que no salieron la vez anterior."""
        if self._notifier is None:
            return

        new_listings = select_new_listings(result, previous)
        if not new_listings:
            self._logger.info(
                "listing_alert_skipped",
                search_id=str(result.search_id),
                reason="no_new_listings",
            )
            return

        digest = build_digest(
            new_listings,
            result.criteria,
            dashboard_url=self._alert_click_url,
        )
        try:
            await self._notifier.send_digest(
                title=digest.title,
                message=digest.message,
                click_url=digest.click_url,
            )
        except Exception:  # una alerta caída no invalida la búsqueda ya guardada
            self._logger.exception(
                "listing_alert_failed",
                search_id=str(result.search_id),
                error_code="NOTIFICATION_FAILURE",
                new_count=len(new_listings),
            )
            return

        self._logger.info(
            "listing_alert_sent",
            search_id=str(result.search_id),
            new_count=len(new_listings),
            total_count=result.matched_count,
        )

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
