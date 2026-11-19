"""Caso de uso: búsqueda diaria programada e idempotente.

El job se registra con APScheduler en el ``lifespan`` de FastAPI. Toda excepción se
captura dentro del propio job: una tarea programada que lanza pierde el error y no
deja rastro en los handlers HTTP.
"""

from __future__ import annotations

import time
from datetime import date
from typing import Any, Awaitable, Callable, Sequence

from app.application.services.listing_alerts import build_digest, select_new_listings
from app.domain.exceptions import DomainException, RepositoryException
from app.domain.models import SearchCriteria, SearchResult
from app.domain.ports import (
    AlertNotifierPort,
    EmailNotificationStatePort,
    ListingRepository,
    SearchServiceProtocol,
)
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
        notifiers: Sequence[AlertNotifierPort] = (),
        email_notification_state: EmailNotificationStatePort | None = None,
        alert_click_url: str | None = None,
        logger: Any | None = None,
        today: Callable[[], date] | None = None,
    ) -> None:
        self._search_service = search_service
        self._repository = repository
        self._criteria = criteria
        # El perfil puede cambiar desde la UI: se resuelve al inicio de cada ejecución.
        self._criteria_provider = criteria_provider
        # Cada canal es independiente: un fallo no impide intentar los demás.
        self._notifiers = tuple(notifiers)
        self._email_notification_state = email_notification_state
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
        """Envía el resumen de viviendas ausentes de la ejecución anterior."""
        if not self._notifiers:
            return

        new_listings = select_new_listings(result, previous)
        initial_email_digest = False
        if self._email_notification_state is not None and any(
            notifier.channel == "email" for notifier in self._notifiers
        ):
            try:
                initial_email_digest = (
                    not await self._email_notification_state.has_sent_initial_digest()
                )
            except RepositoryException as exc:
                self._logger.warning(
                    "email_initial_digest_state_read_failed",
                    error_code=exc.code,
                    search_id=str(result.search_id),
                    channel="email",
                )
                initial_email_digest = True

        if not new_listings and not (initial_email_digest and result.listings):
            self._logger.info(
                "listing_alert_skipped",
                search_id=str(result.search_id),
                reason="no_new_listings",
            )
            return

        for notifier in self._notifiers:
            listings_to_send = (
                result.listings
                if notifier.channel == "email" and initial_email_digest
                else new_listings
            )
            if not listings_to_send:
                continue

            if notifier.channel == "email" and initial_email_digest:
                digest = build_digest(
                    listings_to_send,
                    result.criteria,
                    dashboard_url=self._alert_click_url,
                    max_listings=len(listings_to_send),
                )
            else:
                digest = build_digest(
                    listings_to_send,
                    result.criteria,
                    dashboard_url=self._alert_click_url,
                )

            try:
                await notifier.send_digest(
                    title=digest.title,
                    message=digest.message,
                    click_url=digest.click_url,
                    html_message=digest.html_message,
                )
            except Exception:  # un canal caído no invalida el resultado ni los demás
                self._logger.exception(
                    "listing_alert_failed",
                    search_id=str(result.search_id),
                    channel=notifier.channel,
                    error_code="NOTIFICATION_FAILURE",
                    new_count=len(new_listings),
                    sent_count=len(listings_to_send),
                )
                continue

            if (
                notifier.channel == "email"
                and initial_email_digest
                and self._email_notification_state is not None
            ):
                try:
                    await self._email_notification_state.mark_initial_digest_sent()
                except RepositoryException as exc:
                    self._logger.warning(
                        "email_initial_digest_state_write_failed",
                        error_code=exc.code,
                        search_id=str(result.search_id),
                        channel="email",
                        sent_count=len(listings_to_send),
                    )

            self._logger.info(
                "listing_alert_sent",
                search_id=str(result.search_id),
                channel=notifier.channel,
                new_count=len(new_listings),
                sent_count=len(listings_to_send),
                total_count=result.matched_count,
                initial_digest=notifier.channel == "email" and initial_email_digest,
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
