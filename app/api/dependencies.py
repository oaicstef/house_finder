"""Composición de dependencias (composition root) e inyección con FastAPI.

``build_container`` es el único sitio que conoce las implementaciones concretas; los
endpoints dependen de los puertos a través de ``Depends``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Request

from app.application.services.scheduled_search import ScheduledSearchService
from app.application.services.search_profile import (
    ProfileSource,
    SearchProfileService,
)
from app.application.services.search_service import SearchService
from app.config import Settings, get_settings
from app.domain.exceptions import ConfigurationException
from app.domain.models import SearchCriteria
from app.domain.ports import (
    AlertNotifierPort,
    ClassifierPort,
    ListingRepository,
    ScheduledSearchServiceProtocol,
    ScraperPort,
    SearchProfileStore,
    SearchServiceProtocol,
)
from app.infrastructure.alerts.ntfy_notifier import NtfyNotifier
from app.infrastructure.classifiers.deepseek_classifier import DeepSeekListingClassifier
from app.infrastructure.geocoding.carto_ciudad_locator import CartoCiudadPostalCodeLocator
from app.infrastructure.repositories.json_file_repository import JsonFileListingRepository
from app.infrastructure.repositories.memory_repository import InMemoryListingRepository
from app.infrastructure.logging_config import get_logger
from app.infrastructure.repositories.search_profile_store import (
    InMemorySearchProfileStore,
    JsonSearchProfileStore,
)
from app.infrastructure.scrapers.factory import build_scraper

__all__ = [
    "ContainerDep",
    "ScheduledSearchServiceDep",
    "SearchProfileServiceDep",
    "SearchServiceDep",
    "ServiceContainer",
    "build_container",
    "get_container",
    "get_scheduled_search_service",
    "get_search_profile_service",
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
    search_profile_service: SearchProfileService
    search_profile_store: SearchProfileStore
    #: ``None`` cuando ``ALERTS_ENABLED=false`` (el job diario no envía nada).
    alert_notifier: AlertNotifierPort | None = None


def build_container(settings: Settings | None = None) -> ServiceContainer:
    """Construye el grafo de dependencias con las implementaciones reales."""
    resolved = settings or get_settings()

    repository: ListingRepository
    profile_store: SearchProfileStore
    if (resolved.results_dir or "").strip():
        repository = JsonFileListingRepository(resolved.results_dir)
        profile_store = JsonSearchProfileStore(
            resolved.resolved_search_profile_file or "search-profile.json"
        )
    else:
        repository = InMemoryListingRepository()
        profile_store = InMemorySearchProfileStore()

    # El proveedor por portal lo decide ``SCRAPER_PROVIDERS`` (local | apify | none).
    scraper: ScraperPort = build_scraper(resolved)
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

    def default_profile() -> tuple[SearchCriteria, ProfileSource]:
        """Perfil de configuración y su origen (``SEARCH_PROFILE_JSON`` o ``DAILY_*``)."""
        source: ProfileSource = (
            "legacy" if resolved.build_search_profile() is None else "profile"
        )
        return resolved.build_daily_criteria(), source

    profile_service = SearchProfileService(profile_store, default_profile)

    # Alertas push de viviendas nuevas (ntfy: app móvil iOS y Android).
    alert_notifier: AlertNotifierPort | None = None
    if resolved.alerts_enabled:
        alert_notifier = NtfyNotifier(
            resolved.ntfy_topic,
            base_url=resolved.ntfy_base_url,
            token=resolved.ntfy_token,
            priority=resolved.ntfy_priority,
            logger=get_logger("app.alerts"),
        )

    scheduled_search_service = ScheduledSearchService(
        search_service,
        repository,
        resolved.build_daily_criteria(),
        # La UI puede cambiar el perfil: el cron lo resuelve en cada ejecución.
        criteria_provider=profile_service.resolve_criteria,
        notifier=alert_notifier,
        alert_click_url=resolved.alert_click_url,
    )

    return ServiceContainer(
        settings=resolved,
        repository=repository,
        scraper=scraper,
        classifier=classifier,
        search_service=search_service,
        scheduled_search_service=scheduled_search_service,
        search_profile_service=profile_service,
        search_profile_store=profile_store,
        alert_notifier=alert_notifier,
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


def get_search_profile_service(
    container: Annotated[ServiceContainer, Depends(get_container)],
) -> SearchProfileService:
    return container.search_profile_service


SearchProfileServiceDep = Annotated[
    SearchProfileService,
    Depends(get_search_profile_service),
]
