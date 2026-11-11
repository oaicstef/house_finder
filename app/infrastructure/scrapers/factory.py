"""Composición del scraper a partir de ``SCRAPER_PROVIDERS``.

``build_scraper`` es el único sitio que decide qué proveedor sirve cada portal:
HTTP propio (``local``), actores de Apify (``apify``) o nada (``none``). La API y
los servicios siguen viendo un ``ScraperPort``, así que cambiar de proveedor es
configuración, no código.
"""

from __future__ import annotations

from typing import Any

from app.config import Settings
from app.domain.exceptions import ConfigurationException
from app.domain.models import Portal
from app.domain.ports import ScraperPort
from app.infrastructure.logging_config import get_logger
from app.infrastructure.scrapers.apify_scraper import ApifyListingScraper
from app.infrastructure.scrapers.listing_scraper import ListingScraper
from app.infrastructure.scrapers.sources.fotocasa import FotocasaHttpSource
from app.infrastructure.scrapers.sources.http import DEFAULT_USER_AGENT, HttpClient
from app.infrastructure.scrapers.sources.idealista_api import IdealistaApiSource

__all__ = ["build_scraper"]

#: Portales con implementación propia mediante HTTP.
_LOCAL_PORTALS = frozenset({Portal.FOTOCASA})


def _actor_id(settings: Settings, portal: Portal) -> str:
    if portal is Portal.IDEALISTA:
        return settings.apify_idealista_actor
    if portal is Portal.FOTOCASA:
        return settings.apify_fotocasa_actor
    return ""


def build_scraper(settings: Settings, logger: Any | None = None) -> ScraperPort:
    """Devuelve el scraper que corresponde a la configuración.

    Raises:
        ConfigurationException: si el mapa de proveedores es incoherente o falta
            ``APIFY_TOKEN`` para un portal que lo necesita.
    """
    resolved_logger = logger or get_logger("app.scraper")
    providers = settings.scraper_providers_map
    local_portals = [portal for portal, provider in providers.items() if provider == "local"]
    idealista_api_portals = [
        portal for portal, provider in providers.items() if provider == "idealista-api"
    ]
    source_portals = local_portals + idealista_api_portals
    apify_portals = [portal for portal, provider in providers.items() if provider == "apify"]

    for portal in local_portals:
        if portal not in _LOCAL_PORTALS:
            raise ConfigurationException(
                f"El portal {portal.value!r} no tiene scraper propio: "
                f"usa SCRAPER_PROVIDERS={portal.value}=apify",
                details={
                    "portal": portal.value,
                    "providers": {p.value: v for p, v in providers.items()},
                },
            )
    invalid_api_portals = [
        portal for portal in idealista_api_portals if portal is not Portal.IDEALISTA
    ]
    if invalid_api_portals:
        raise ConfigurationException(
            "El proveedor idealista-api solo puede configurarse para el portal idealista",
            details={"portals": [portal.value for portal in invalid_api_portals]},
        )

    token = (settings.apify_token or "").strip()
    if apify_portals and not token:
        raise ConfigurationException(
            "APIFY_TOKEN es obligatorio cuando un portal usa el proveedor apify",
            details={"portals": [portal.value for portal in apify_portals]},
        )

    apify_scraper: ApifyListingScraper | None = None
    if token:
        apify_scraper = ApifyListingScraper(
            token=token,
            actor_ids={portal: _actor_id(settings, portal) for portal in providers},
            max_items=settings.apify_max_items,
            timeout_seconds=settings.apify_timeout_seconds,
            operation=settings.search_operation,
            max_charge_usd=settings.apify_max_charge_usd,
            logger=resolved_logger,
        )

    if not source_portals:
        if apify_scraper is None:
            raise ConfigurationException(
                "Ningún portal tiene un proveedor de scraping utilizable",
                details={"providers": {p.value: v for p, v in providers.items()}},
            )
        resolved_logger.info(
            "scraper_provider_selected",
            providers={portal.value: providers[portal] for portal in providers},
            fallback=False,
        )
        return apify_scraper

    http_client = HttpClient(
        user_agent=(settings.scraper_user_agent or "").strip() or DEFAULT_USER_AGENT,
        timeout_seconds=settings.scraper_timeout_seconds,
        max_retries=settings.scraper_max_retries,
        rate_limit_per_minute=settings.scraper_rate_limit_per_minute,
        proxy_url=settings.scraper_proxy_url,
        logger=resolved_logger,
    )
    sources = {
        portal: FotocasaHttpSource(
            client=http_client,
            operation=settings.search_operation,
            max_items=settings.scraper_max_items,
            max_pages=settings.scraper_max_pages,
            location_override=settings.scraper_fotocasa_location_segment,
            logger=resolved_logger,
        )
        for portal in local_portals
    }
    for portal in idealista_api_portals:
        sources[portal] = IdealistaApiSource(
            client=http_client,
            client_id=settings.idealista_api_client_id or "",
            client_secret=settings.idealista_api_client_secret or "",
            operation=settings.search_operation,
            max_items=settings.scraper_max_items,
            max_pages=settings.scraper_max_pages,
            logger=resolved_logger,
        )

    delegates: dict[Portal, ScraperPort] = {}
    if apify_scraper is not None:
        for portal in apify_portals:
            delegates[portal] = apify_scraper
        if settings.scraper_fallback_to_apify:
            for portal in source_portals:
                delegates[portal] = apify_scraper
    elif settings.scraper_fallback_to_apify:
        resolved_logger.warning(
            "scraper_fallback_disabled",
            error_code=None,
            reason="APIFY_TOKEN no configurado",
        )

    resolved_logger.info(
        "scraper_provider_selected",
        providers={portal.value: providers[portal] for portal in providers},
        fallback=settings.scraper_fallback_to_apify and apify_scraper is not None,
    )
    return ListingScraper(
        sources=sources,
        delegates=delegates,
        logger=resolved_logger,
        fallback_on_empty=settings.scraper_fallback_to_apify,
    )
