"""Composición de dependencias (composition root) e inyección con FastAPI.

``build_container`` es el único sitio que conoce las implementaciones concretas; los
endpoints dependen de los puertos a través de ``Depends``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Request

from app.application.services.scheduled_search import ScheduledSearchService
from app.application.services.search_service import SearchService
from app.config import Settings, get_settings
from app.domain.exceptions import ConfigurationException
from app.domain.models import Portal
from app.domain.ports import (
    ClassifierPort,
    ListingRepository,
    ScheduledSearchServiceProtocol,
    ScraperPort,
    SearchServiceProtocol,
)
from app.infrastructure.classifiers.deepseek_classifier import DeepSeekListingClassifier
from app.infrastructure.geocoding.carto_ciudad_locator import CartoCiudadPostalCodeLocator
from app.infrastructure.repositories.json_file_repository import JsonFileListingRepository
from app.infrastructure.repositories.memory_repository import InMemoryListingRepository
from app.infrastructure.scrapers.apify_scraper import ApifyListingScraper

__all__ = [
    "ContainerDep",
    "ScheduledSearchServiceDep",
    "SearchServiceDep",
    "ServiceContainer",
    "build_container",
    "get_container",
    "get_scheduled_search_service",
    "get_search_service",
]


@dataclass(slots=True)
class ServiceContainer:
    """Grafo de dependencias ya resuelto para toda la vida de la aplicación."""

    settings: Settings
    repository: ListingRepository
    scraper: ScraperPort
    classifier: ClassifierPort
    search_service: SearchServiceProtocol
    scheduled_search_service: ScheduledSearchServiceProtocol


def build_container(settings: Settings | None = None) -> ServiceContainer:
    """Construye el grafo de dependencias con las implementaciones reales."""
    resolved = settings or get_settings()

    repository: ListingRepository
    if (resolved.results_dir or "").strip():
        repository = JsonFileListingRepository(resolved.results_dir)
    else:
        repository = InMemoryListingRepository()

    scraper: ScraperPort = ApifyListingScraper(
        token=resolved.apify_token,
        actor_ids={
            Portal.IDEALISTA: resolved.apify_idealista_actor,
            Portal.FOTOCASA: resolved.apify_fotocasa_actor,
        },
        max_items=resolved.apify_max_items,
        timeout_seconds=resolved.apify_timeout_seconds,
        operation=resolved.search_operation,
        max_charge_usd=resolved.apify_max_charge_usd,
    )
    classifier: ClassifierPort = DeepSeekListingClassifier(
        api_key=resolved.deepseek_api_key,
        model=resolved.deepseek_model,
        base_url=resolved.deepseek_base_url,
        timeout_seconds=resolved.deepseek_timeout_seconds,
        relevance_threshold=resolved.relevance_threshold,
    )

    postal_code_locator = CartoCiudadPostalCodeLocator(
        base_url=resolved.geocoding_base_url,
        timeout_seconds=resolved.geocoding_timeout_seconds,
    )
    search_service = SearchService(
        scraper,
        classifier,
        repository,
        postal_code_locator=postal_code_locator,
    )
    scheduled_search_service = ScheduledSearchService(
        search_service,
        repository,
        resolved.build_daily_criteria(),
    )

    return ServiceContainer(
        settings=resolved,
        repository=repository,
        scraper=scraper,
        classifier=classifier,
        search_service=search_service,
        scheduled_search_service=scheduled_search_service,
    )


def get_container(request: Request) -> ServiceContainer:
    """Recupera el contenedor creado en el ``lifespan`` de la aplicación."""
    container = getattr(request.app.state, "container", None)
    if container is None:
        raise ConfigurationException(
            "El contenedor de servicios no está inicializado",
            details={"hint": "El lifespan de la aplicación no se ha ejecutado"},
        )
    return container


def get_search_service(
    container: Annotated[ServiceContainer, Depends(get_container)],
) -> SearchServiceProtocol:
    return container.search_service


def get_scheduled_search_service(
    container: Annotated[ServiceContainer, Depends(get_container)],
) -> ScheduledSearchServiceProtocol:
    return container.scheduled_search_service


ContainerDep = Annotated[ServiceContainer, Depends(get_container)]
SearchServiceDep = Annotated[SearchServiceProtocol, Depends(get_search_service)]
ScheduledSearchServiceDep = Annotated[
    ScheduledSearchServiceProtocol,
    Depends(get_scheduled_search_service),
]
