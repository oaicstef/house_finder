"""Fixtures y dobles de test compartidos.

Ningún test toca Apify ni DeepSeek: los puertos se sustituyen por fakes en memoria.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, Sequence

import pytest

from app.api.dependencies import ServiceContainer
from app.application.services.scheduled_search import ScheduledSearchService
from app.application.services.search_profile import SearchProfileService
from app.application.services.search_service import SearchService
from app.config import Settings
from app.domain.exceptions import ClassifierException, ScraperException
from app.domain.models import (
    Listing,
    ListingAssessment,
    Portal,
    SearchCriteria,
)
from app.infrastructure.repositories.memory_repository import InMemoryListingRepository
from app.infrastructure.repositories.search_profile_store import InMemorySearchProfileStore


class RecordingLogger:
    """Logger de test que guarda los eventos emitidos (sin structlog)."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def _record(self, level: str, event: str, **kwargs: Any) -> None:
        self.events.append({"level": level, "event": event, **kwargs})

    def debug(self, event: str, **kwargs: Any) -> None:
        self._record("debug", event, **kwargs)

    def info(self, event: str, **kwargs: Any) -> None:
        self._record("info", event, **kwargs)

    def warning(self, event: str, **kwargs: Any) -> None:
        self._record("warning", event, **kwargs)

    def error(self, event: str, **kwargs: Any) -> None:
        self._record("error", event, **kwargs)

    def exception(self, event: str, **kwargs: Any) -> None:
        self._record("exception", event, **kwargs)

    def event_names(self) -> list[str]:
        return [event["event"] for event in self.events]

    def find(self, name: str) -> dict[str, Any] | None:
        return next((event for event in self.events if event["event"] == name), None)


class FakeScraper:
    """Implementación en memoria de ``ScraperPort``."""

    def __init__(
        self,
        listings: Sequence[Listing] | None = None,
        *,
        error: Exception | None = None,
    ) -> None:
        self.listings = list(listings or [])
        self.error = error
        self.calls: list[SearchCriteria] = []

    async def fetch_listings(self, criteria: SearchCriteria) -> list[Listing]:
        self.calls.append(criteria)
        if self.error is not None:
            raise self.error
        return list(self.listings)


class FakeClassifier:
    """Implementación en memoria de ``ClassifierPort``."""

    def __init__(
        self,
        assessments: Sequence[ListingAssessment] | None = None,
        *,
        error: Exception | None = None,
        relevance: float = 0.9,
    ) -> None:
        self.assessments = list(assessments) if assessments is not None else None
        self.error = error
        self.relevance = relevance
        self.calls: list[tuple[SearchCriteria, list[Listing]]] = []

    async def assess_listings(
        self,
        criteria: SearchCriteria,
        listings: Sequence[Listing],
    ) -> list[ListingAssessment]:
        self.calls.append((criteria, list(listings)))
        if self.error is not None:
            raise self.error
        if self.assessments is not None:
            return list(self.assessments)
        return [
            ListingAssessment(
                listing_url=listing.url,
                relevance=self.relevance,
                matches=True,
                reason="Encaja con los criterios",
            )
            for listing in listings
        ]


class ExplodingRepository(InMemoryListingRepository):
    """Repositorio que falla al comprobar idempotencia (para el entrypoint)."""

    async def find_by_criteria_and_day(
        self,
        criteria_fingerprint: str,
        run_date: date,
    ) -> None:
        raise RuntimeError("fallo inesperado de almacenamiento")


def make_listing(
    url: str = "https://www.idealista.com/inmueble/1/",
    *,
    price: float = 1200.0,
    size: float | None = 80.0,
    rooms: int | None = 2,
    portal: Portal = Portal.IDEALISTA,
    title: str = "Piso luminoso en Chamberí",
    year_built: int | None = None,
    description: str = "Piso exterior con ascensor",
    latitude: float | None = None,
    longitude: float | None = None,
    photos: tuple[str, ...] = (),
) -> Listing:
    return Listing(
        portal=portal,
        url=url,
        title=title,
        price_eur=price,
        location="Madrid",
        size_m2=size,
        rooms=rooms,
        bathrooms=1,
        description=description,
        year_built=year_built,
        latitude=latitude,
        longitude=longitude,
        photos=photos,
    )


@pytest.fixture
def settings() -> Settings:
    return Settings(environment="test", enable_scheduler=False, log_level="INFO")


@pytest.fixture
def criteria() -> SearchCriteria:
    return SearchCriteria(location="Madrid", max_price_eur=1500.0, min_rooms=2)


@pytest.fixture
def repository() -> InMemoryListingRepository:
    return InMemoryListingRepository(logger=RecordingLogger())


@pytest.fixture
def logger() -> RecordingLogger:
    return RecordingLogger()


@pytest.fixture
def make_container(settings: Settings) -> Any:
    """Construye un ``ServiceContainer`` con fakes para los tests de API."""

    def _build(
        *,
        scraper: FakeScraper | None = None,
        classifier: FakeClassifier | None = None,
        repository: InMemoryListingRepository | None = None,
        search_criteria: SearchCriteria | None = None,
        postal_code_locator: Any = None,
        today: Any = None,
        profile_store: InMemorySearchProfileStore | None = None,
    ) -> ServiceContainer:
        repo = repository or InMemoryListingRepository(logger=RecordingLogger())
        scraper_port = scraper or FakeScraper([make_listing()])
        classifier_port = classifier or FakeClassifier()
        search_service = SearchService(
            scraper_port,
            classifier_port,
            repo,
            postal_code_locator=postal_code_locator,
            logger=RecordingLogger(),
            today=today,
        )
        default_criteria = search_criteria or SearchCriteria(location="Madrid")

        def default_profile() -> tuple[SearchCriteria, str]:
            # Perfil "de fábrica" del fixture: el de ``settings`` si lo hay.
            if search_criteria is not None:
                return search_criteria, "legacy"
            if settings.build_search_profile() is None:
                return settings.build_daily_criteria(), "legacy"
            return settings.build_daily_criteria(), "profile"

        store = profile_store or InMemorySearchProfileStore()
        profile_service = SearchProfileService(
            store,
            default_profile,  # type: ignore[arg-type]
            logger=RecordingLogger(),
        )

        async def scheduled_criteria() -> SearchCriteria:
            """El cron usa el override de la UI o su criterio por defecto."""
            saved = await store.get()
            return saved if saved is not None else default_criteria

        scheduled_service = ScheduledSearchService(
            search_service,
            repo,
            default_criteria,
            criteria_provider=scheduled_criteria,
            logger=RecordingLogger(),
            today=today,
        )
        return ServiceContainer(
            settings=settings,
            repository=repo,
            scraper=scraper_port,
            classifier=classifier_port,
            search_service=search_service,
            scheduled_search_service=scheduled_service,
            search_profile_service=profile_service,
            search_profile_store=store,
        )

    return _build


__all__ = [
    "ExplodingRepository",
    "FakeClassifier",
    "FakeScraper",
    "RecordingLogger",
    "make_listing",
]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def scraper_failure() -> ScraperException:
    return ScraperException("Apify caído", details={"portal": "idealista"})


def classifier_failure() -> ClassifierException:
    return ClassifierException("DeepSeek no responde")
