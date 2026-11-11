"""Orquestación de scraping independiente del proveedor.

``ListingScraper`` implementa ``ScraperPort`` a partir de una **fuente por portal**
(HTTP propio, actores de Apify…) y reproduce el comportamiento histórico del
adaptador de Apify: dedupe por URL, filtros duros, enriquecimiento de descripciones
y los mismos eventos de log. Cambiar de proveedor no cambia la respuesta de la API.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Mapping

from app.domain.exceptions import ScraperException
from app.domain.models import ENERGY_RATING_SCALE, Listing, Portal, SearchCriteria
from app.domain.ports import ScraperPort
from app.infrastructure.logging_config import get_logger
from app.infrastructure.scrapers.filtering import distance_km, matches_hard_criteria
from app.infrastructure.scrapers.normalization import _PROFILES, normalize_listing
from app.infrastructure.scrapers.sources.base import RawListingSource

__all__ = ["ListingScraper"]


def _provider_name(source: Any) -> str:
    """Nombre legible del proveedor para logs (``apify``, ``fotocasa-http``…)."""
    return str(getattr(source, "name", type(source).__name__))


class ListingScraper:
    """Scraper compuesto: una fuente por portal y proveedores de respaldo opcionales."""

    #: Motivo registrado cuando un portal solicitado no tiene fuente.
    missing_source_reason = "source_not_configured"
    #: Mensaje cuando ningún portal solicitado tiene fuente.
    empty_message = "Ningún portal tiene una fuente de scraping configurada"

    def __init__(
        self,
        *,
        sources: Mapping[Portal, RawListingSource] | None = None,
        delegates: Mapping[Portal, ScraperPort] | None = None,
        logger: Any | None = None,
        fallback_on_empty: bool = False,
    ) -> None:
        self._sources = dict(sources or {})
        #: Portales servidos por otro scraper (p. ej. Idealista por Apify) y, a la
        #: vez, red de seguridad si la fuente local falla.
        self._delegates = dict(delegates or {})
        self._logger = logger or get_logger("app.scraper")
        self._fallback_on_empty = fallback_on_empty
        #: Proveedor usado por portal en la última ejecución (diagnóstico).
        self.last_provider: dict[Portal, str] = {}

    #: Proveedor local registrado por portal (diagnóstico / tests).
    @property
    def source_names(self) -> dict[Portal, str]:
        return {portal: _provider_name(source) for portal, source in self._sources.items()}

    #: Proveedor alternativo registrado por portal (diagnóstico / tests).
    @property
    def delegate_names(self) -> dict[Portal, str]:
        return {portal: _provider_name(delegate) for portal, delegate in self._delegates.items()}

    # ----------------------------------------------------------- fuentes ---

    def _has_source(self, portal: Portal) -> bool:
        """¿Hay alguien que pueda servir este portal?"""
        return portal in self._sources or portal in self._delegates

    async def _fetch_portal(
        self, portal: Portal, criteria: SearchCriteria
    ) -> list[Mapping[str, Any]]:
        """Items crudos del portal (una ejecución de la fuente local)."""
        source = self._sources[portal]
        return await source.fetch(criteria)

    async def _enrich_descriptions(self, listings: list[Listing]) -> list[Listing]:
        """Pide el detalle a las fuentes locales que lo soporten (Idealista)."""
        enriched_by_url: dict[str, Listing] = {}
        for portal in {listing.portal for listing in listings}:
            source = self._sources.get(portal)
            if source is None:
                # Portal servido por otro scraper: ya trae sus detalles.
                continue
            group = [listing for listing in listings if listing.portal is portal]
            try:
                enriched = await source.enrich(group)
            except ScraperException as exc:
                self._logger.warning(
                    "listing_details_failed",
                    portal=portal.value,
                    error_code=exc.code,
                    candidate_count=len(group),
                )
                continue
            enriched_by_url.update({listing.url: listing for listing in enriched})
        if not enriched_by_url:
            return listings
        # Mantiene el orden original de la lista.
        return [enriched_by_url.get(listing.url, listing) for listing in listings]

    async def _append_delegate(
        self,
        portal: Portal,
        criteria: SearchCriteria,
        listings: list[Listing],
        seen_urls: set[str],
        *,
        reason: str,
    ) -> None:
        """Añade los resultados de otro scraper para un portal concreto."""
        delegate = self._delegates[portal]
        self._logger.warning(
            "scraper_fallback_used",
            portal=portal.value,
            provider=_provider_name(delegate),
            reason=reason,
        )
        portal_criteria = replace(criteria, portals=(portal,))
        for listing in await delegate.fetch_listings(portal_criteria):
            if listing.url in seen_urls:
                continue
            seen_urls.add(listing.url)
            listings.append(listing)
        self.last_provider[portal] = _provider_name(delegate)

    async def aclose(self) -> None:
        """Cierra recursos de red de las fuentes (si los abren)."""
        for source in self._sources.values():
            close = getattr(source, "aclose", None)
            if close is None:
                continue
            await close()

    # ------------------------------------------------------------- port ---

    async def fetch_listings(self, criteria: SearchCriteria) -> list[Listing]:
        """Devuelve los anuncios que cumplen los criterios duros."""
        listings: list[Listing] = []
        seen_urls: set[str] = set()
        skipped: list[str] = []

        for portal in criteria.portals:
            if not self._has_source(portal):
                # Un portal sin fuente configurada no debe tumbar la búsqueda entera.
                skipped.append(portal.value)
                self._logger.warning(
                    "scraper_portal_skipped",
                    portal=portal.value,
                    error_code=None,
                    reason=self.missing_source_reason,
                )
                continue

            if criteria.home_types and not _PROFILES[portal].supports_home_type:
                # Fotocasa publica subtipos numéricos sin mapeo fiable: se omite
                # antes que colar pisos cuando el usuario ha elegido tipos.
                skipped.append(portal.value)
                self._logger.warning(
                    "scraper_portal_skipped",
                    portal=portal.value,
                    error_code=None,
                    reason="home_type_unsupported",
                )
                continue

            if portal not in self._sources:
                # Portal servido íntegramente por otro proveedor (p. ej. Apify).
                await self._append_delegate(
                    portal, criteria, listings, seen_urls, reason="provider"
                )
                continue

            source = self._sources[portal]
            try:
                items = await self._fetch_portal(portal, criteria)
            except ScraperException as exc:
                if portal not in self._delegates:
                    raise
                self._logger.warning(
                    "search_scraper_failed",
                    portal=portal.value,
                    provider=_provider_name(source),
                    error_code=exc.code,
                    error_type=type(exc).__name__,
                )
                await self._append_delegate(
                    portal, criteria, listings, seen_urls, reason="error"
                )
                continue

            if not items and self._fallback_on_empty and portal in self._delegates:
                await self._append_delegate(
                    portal, criteria, listings, seen_urls, reason="empty"
                )
                continue

            self.last_provider[portal] = _provider_name(source)

            portal_kept = 0
            radius_dropped = 0
            radius_unknown = 0
            year_dropped = 0
            year_unknown = 0
            energy_dropped = 0
            energy_unknown = 0
            home_type_dropped = 0
            radius_km = criteria.radius_km
            geo_enabled = (
                radius_km is not None
                and criteria.latitude is not None
                and criteria.longitude is not None
            )
            for item in items:
                listing = normalize_listing(portal, item)
                if listing is None or listing.url in seen_urls:
                    continue

                if geo_enabled:
                    listing_distance_km = distance_km(listing, criteria)
                    if listing_distance_km is None:
                        radius_unknown += 1
                    elif radius_km is not None and listing_distance_km > radius_km:
                        radius_dropped += 1
                        continue

                if criteria.home_types and listing.home_type not in criteria.home_types:
                    home_type_dropped += 1
                    continue

                if criteria.min_year_built is not None:
                    if listing.year_built is None:
                        year_unknown += 1
                    elif listing.year_built < criteria.min_year_built:
                        year_dropped += 1
                        continue

                if criteria.max_energy_rating is not None:
                    if listing.energy_rating is None:
                        # Sin certificado no se puede garantizar el mínimo: fuera.
                        energy_unknown += 1
                        continue
                    if ENERGY_RATING_SCALE.index(
                        listing.energy_rating
                    ) > ENERGY_RATING_SCALE.index(criteria.max_energy_rating):
                        energy_dropped += 1
                        continue

                if not matches_hard_criteria(listing, criteria):
                    continue
                seen_urls.add(listing.url)
                listings.append(listing)
                portal_kept += 1

            if geo_enabled:
                self._logger.info(
                    "listings_filtered_by_radius",
                    portal=portal.value,
                    radius_km=criteria.radius_km,
                    kept_count=portal_kept,
                    dropped_count=radius_dropped,
                    unknown_coordinates_count=radius_unknown,
                )
            if criteria.min_year_built is not None:
                self._logger.info(
                    "listings_filtered_by_year",
                    portal=portal.value,
                    min_year_built=criteria.min_year_built,
                    kept_count=portal_kept,
                    dropped_count=year_dropped,
                    unknown_year_count=year_unknown,
                )
            if criteria.home_types:
                self._logger.info(
                    "listings_filtered_by_home_type",
                    portal=portal.value,
                    home_types=list(criteria.home_types),
                    kept_count=portal_kept,
                    dropped_count=home_type_dropped,
                )
            if criteria.max_energy_rating is not None:
                self._logger.info(
                    "listings_filtered_by_energy",
                    portal=portal.value,
                    max_energy_rating=criteria.max_energy_rating,
                    kept_count=portal_kept,
                    dropped_count=energy_dropped,
                    unknown_rating_count=energy_unknown,
                )

        if skipped and len(skipped) == len(criteria.portals):
            raise ScraperException(
                self.empty_message,
                details={"portals": skipped},
            )

        if listings and criteria.max_energy_rating is None:
            # Sin filtro energético, el listado no trae ``_details``. Se piden para
            # los candidatos ya filtrados: de ahí salen la etiqueta energética y las
            # versiones lingüísticas donde puede aparecer un término preferente.
            listings = await self._enrich_descriptions(listings)

        # Se devuelven TODOS los candidatos (acotados por ``max_items``): recortar aquí
        # a ``max_results`` impediría que los filtros posteriores (must_have) y el
        # clasificador vieran más opciones que las estrictamente devueltas al cliente.
        return listings
