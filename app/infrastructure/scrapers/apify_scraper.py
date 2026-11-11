"""Adaptador de scraping basado en Apify (proveedor opcional / de respaldo).

Implementa ``ScraperPort`` delegando en un actor de Apify por portal. La ejecución
del actor vive en ``sources/apify.py``; aquí solo se compone una fuente por portal
sobre el núcleo común (``ListingScraper``), que aplica normalización y filtros.

Con el scraper propio activo este adaptador sigue siendo útil como **red de
seguridad** por portal (``SCRAPER_FALLBACK_TO_APIFY``) hasta retirarlo del todo.
"""

from __future__ import annotations

from typing import Any, Callable, Mapping

from app.domain.models import Portal
from app.infrastructure.logging_config import get_logger
from app.infrastructure.scrapers.listing_scraper import ListingScraper
from app.infrastructure.scrapers.normalization import _to_float  # noqa: F401  (API histórica)
from app.infrastructure.scrapers.sources.apify import (
    ApifyPortalSource,
    _default_client_factory,
)

__all__ = ["ApifyListingScraper"]


class ApifyListingScraper(ListingScraper):
    """Scraper multi-portal sobre actores de Apify."""

    #: Nombre del proveedor para logs y diagnóstico.
    name = "apify"
    #: Un portal sin actor configurado se omite sin tumbar la búsqueda entera.
    missing_source_reason = "actor_not_configured"
    empty_message = "Ningún portal tiene un actor de Apify configurado"

    def __init__(
        self,
        *,
        token: str,
        actor_ids: Mapping[Portal, str],
        max_items: int = 40,
        timeout_seconds: int = 300,
        operation: str = "rent",
        max_charge_usd: float | None = None,
        client_factory: Callable[[str], Any] = _default_client_factory,
        logger: Any | None = None,
    ) -> None:
        resolved_logger = logger or get_logger("app.scraper")
        sources = {
            portal: ApifyPortalSource(
                portal=portal,
                token=token,
                actor_id=str(actor_id).strip(),
                max_items=max_items,
                timeout_seconds=timeout_seconds,
                operation=operation,
                max_charge_usd=max_charge_usd,
                client_factory=client_factory,
                logger=resolved_logger,
            )
            for portal, actor_id in actor_ids.items()
            if str(actor_id or "").strip()
        }
        super().__init__(sources=sources, logger=resolved_logger)
        #: Compatibilidad: la configuración original sigue accesible para diagnóstico.
        self._actor_ids = dict(actor_ids)
