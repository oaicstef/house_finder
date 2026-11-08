"""Caso de uso: ejecución y consulta de búsquedas inmobiliarias.

Orquesta los puertos (scraping -> clasificación -> persistencia) sin conocer
implementaciones concretas: cumple SRP (solo orquesta) y DIP (depende de ports).
"""

from __future__ import annotations

import time
from dataclasses import replace
from datetime import date, datetime, timezone
from typing import Any, Callable
from uuid import UUID, uuid4

import structlog

from app.domain.exceptions import (
    ConfigurationException,
    ScraperException,
    SearchNotFoundError,
    ValidationError,
)
from app.domain.models import (
    Listing,
    ScoredListing,
    SearchCriteria,
    SearchResult,
    Trigger,
)
from app.domain.ports import (
    ClassifierPort,
    ListingRepository,
    PostalCodeLocatorPort,
    ScraperPort,
)
from app.domain.validation import validate_criteria
from app.infrastructure.logging_config import get_logger

__all__ = ["SearchService"]


class SearchService:
    """Servicio de aplicación para búsquedas síncronas."""

    def __init__(
        self,
        scraper: ScraperPort,
        classifier: ClassifierPort,
        repository: ListingRepository,
        *,
        postal_code_locator: PostalCodeLocatorPort | None = None,
        logger: Any | None = None,
        today: Callable[[], date] | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._scraper = scraper
        self._classifier = classifier
        self._repository = repository
        self._postal_code_locator = postal_code_locator
        self._logger = logger or get_logger("app.search")
        self._today = today or date.today
        self._now = now or (lambda: datetime.now(timezone.utc))

    async def run_search(
        self,
        criteria: SearchCriteria,
        *,
        trigger: str = "api",
    ) -> SearchResult:
        """Ejecuta una búsqueda completa y devuelve solo los anuncios relevantes."""
        self._validate(criteria)

        search_id = uuid4()
        started = time.perf_counter()
        structlog.contextvars.bind_contextvars(search_id=str(search_id))
        self._logger.info(
            "search_started",
            search_id=str(search_id),
            trigger=trigger,
            location=criteria.location,
            portals=[portal.value for portal in criteria.portals],
            max_results=criteria.max_results,
        )

        try:
            fetched = await self._fetch(criteria, search_id, started)
            listings = self._apply_requirements(fetched, criteria, search_id)
            scored = await self._classify(criteria, listings, search_id)

            result = SearchResult(
                search_id=search_id,
                criteria=criteria,
                criteria_fingerprint=criteria.fingerprint(),
                run_date=self._today(),
                created_at=self._now(),
                trigger=Trigger(trigger),
                listings=tuple(scored),
                scraped_count=len(fetched),
                discarded_count=max(len(fetched) - len(scored), 0),
            )

            await self._repository.save(result)
            self._logger.info(
                "search_completed",
                search_id=str(search_id),
                trigger=trigger,
                matched_count=result.matched_count,
                discarded_count=result.discarded_count,
                duration_ms=self._elapsed_ms(started),
            )
            for outcome in result.portal_outcomes:
                if not outcome.is_empty:
                    continue
                # Un portal puede quedar vacío sin lanzar error (ubicación no resuelta,
                # filtros muy estrictos...); sin este aviso el resultado parece completo.
                self._logger.warning(
                    "search_portal_without_results",
                    search_id=str(search_id),
                    portal=outcome.portal.value,
                    location=criteria.location,
                    scraped_count=result.scraped_count,
                    error_code=None,
                )
            return result
        finally:
            structlog.contextvars.unbind_contextvars("search_id")

    async def get_results(self, search_id: UUID) -> SearchResult:
        """Recupera una búsqueda previa o lanza ``SearchNotFoundError``."""
        result = await self._repository.get_by_id(search_id)
        if result is None:
            self._logger.warning(
                "search_not_found",
                search_id=str(search_id),
                error_code=SearchNotFoundError.code,
            )
            raise SearchNotFoundError(f"No existe la búsqueda {search_id}")
        return result

    async def get_latest(self) -> SearchResult:
        """Última búsqueda guardada (la que pinta la UI al abrirse)."""
        started = time.perf_counter()
        result = await self._repository.find_latest()
        if result is None:
            self._logger.warning(
                "latest_search_missing",
                error_code=SearchNotFoundError.code,
            )
            raise SearchNotFoundError("Todavía no hay búsquedas guardadas")
        self._logger.info(
            "latest_search_served",
            search_id=str(result.search_id),
            created_at=result.created_at.isoformat(),
            duration_ms=self._elapsed_ms(started),
        )
        return result

    async def _fetch(
        self,
        criteria: SearchCriteria,
        search_id: UUID,
        started: float,
    ) -> list[Listing]:
        resolved = await self._resolve_geo(criteria, search_id)
        try:
            listings = await self._scraper.fetch_listings(resolved)
        except ScraperException as exc:
            self._logger.warning(
                "search_scraper_failed",
                search_id=str(search_id),
                error_code=exc.code,
                duration_ms=self._elapsed_ms(started),
                details=exc.details,
            )
            raise

        self._logger.info(
            "listings_scraped",
            search_id=str(search_id),
            scraped_count=len(listings),
            portals=[portal.value for portal in criteria.portals],
            duration_ms=self._elapsed_ms(started),
        )
        return listings

    async def _resolve_geo(self, criteria: SearchCriteria, search_id: UUID) -> SearchCriteria:
        """Resuelve el código postal en coordenadas para la llamada al scraper.

        Los criterios originales se conservan en el resultado: la huella de
        idempotencia no depende de cambios en el centroide del geocodificador.
        """
        if criteria.postal_code is None or criteria.latitude is not None:
            return criteria

        if self._postal_code_locator is None:
            raise ConfigurationException(
                "No hay resolvedor de códigos postales configurado",
                details={"field": "postal_code"},
            )

        started = time.perf_counter()
        coordinates = await self._postal_code_locator.locate(criteria.postal_code)
        duration_ms = self._elapsed_ms(started)
        if coordinates is None:
            self._logger.warning(
                "postal_code_not_found",
                search_id=str(search_id),
                postal_code=criteria.postal_code,
                error_code="POSTAL_CODE_NOT_FOUND",
                duration_ms=duration_ms,
            )
            raise ValidationError(
                f"No se ha encontrado el código postal {criteria.postal_code}",
                code="POSTAL_CODE_NOT_FOUND",
                details={"postal_code": criteria.postal_code},
            )

        self._logger.info(
            "postal_code_resolved",
            search_id=str(search_id),
            postal_code=criteria.postal_code,
            radius_km=criteria.radius_km,
            latitude=coordinates.latitude,
            longitude=coordinates.longitude,
            duration_ms=duration_ms,
        )
        return replace(
            criteria,
            latitude=coordinates.latitude,
            longitude=coordinates.longitude,
        )

    def _apply_requirements(
        self,
        listings: list[Listing],
        criteria: SearchCriteria,
        search_id: UUID,
    ) -> list[Listing]:
        """Filtro duro de ``must_have``: aquí sí son obligatorios, no "deseables".

        Se aplica antes del LLM para no gastar tokens en anuncios que el usuario ha
        excluido explícitamente.
        """
        if not criteria.must_have:
            return listings

        kept = [listing for listing in listings if listing.satisfies_all(criteria.must_have)]
        dropped = len(listings) - len(kept)
        self._logger.info(
            "listings_filtered_by_requirements",
            search_id=str(search_id),
            must_have=list(criteria.must_have),
            kept_count=len(kept),
            dropped_count=dropped,
        )
        return kept

    async def _classify(
        self,
        criteria: SearchCriteria,
        listings: list[Listing],
        search_id: UUID,
    ) -> list[ScoredListing]:
        if not listings:
            self._logger.info("listings_classified", search_id=str(search_id), matched_count=0)
            return []

        started = time.perf_counter()
        assessments = await self._classifier.assess_listings(criteria, listings)

        by_url = {listing.url: listing for listing in listings}
        scored: list[ScoredListing] = []
        for assessment in assessments:
            listing = by_url.get(assessment.listing_url)
            if listing is None or not assessment.matches:
                continue
            scored.append(
                ScoredListing(
                    listing=listing,
                    relevance=assessment.relevance,
                    reason=assessment.reason,
                )
            )

        # Mayor relevancia primero; a igualdad, el anuncio más barato.
        scored.sort(key=lambda item: (-item.relevance, item.listing.price_eur))
        limited = scored[: criteria.max_results]

        self._logger.info(
            "listings_classified",
            search_id=str(search_id),
            scraped_count=len(listings),
            matched_count=len(limited),
            duration_ms=self._elapsed_ms(started),
        )
        return limited

    @staticmethod
    def _validate(criteria: SearchCriteria) -> None:
        validate_criteria(criteria)

    @staticmethod
    def _elapsed_ms(started: float) -> float:
        return round((time.perf_counter() - started) * 1000, 2)
