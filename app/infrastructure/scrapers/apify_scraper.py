"""Adaptador de scraping basado en Apify.

Implementa ``ScraperPort`` delegando en un actor de Apify por portal. El cliente de
Apify es síncrono, por lo que cada ejecución se aísla con ``asyncio.to_thread`` para
no bloquear el event loop de FastAPI.
"""

from __future__ import annotations

import asyncio
import re
import time
from typing import Any, Callable, Mapping, Sequence

from app.domain.exceptions import ScraperException
from app.domain.models import Listing, Portal, SearchCriteria
from app.infrastructure.logging_config import get_logger

__all__ = ["ApifyListingScraper"]

#: Claves candidatas en el item crudo del actor -> campo normalizado.
_FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "url": ("url", "link", "propertyUrl", "detailUrl", "href", "property_url"),
    "title": ("title", "name", "propertyTitle", "headline"),
    "price": ("price", "priceEur", "priceAmount", "priceValue", "currentPrice"),
    "size": ("size", "squareMeters", "surface", "surfaceArea", "area", "constructedArea"),
    "rooms": ("rooms", "bedrooms", "roomCount"),
    "bathrooms": ("bathrooms", "bathroomCount"),
    "location": ("location", "address", "city", "district", "place", "neighborhood"),
    "description": ("description", "body", "summary", "details"),
}

_NUMBER_TOKEN = re.compile(r"-?\d[\d.,]*")


def _first_value(item: Mapping[str, Any], keys: Sequence[str]) -> Any | None:
    for key in keys:
        value = item.get(key)
        if value not in (None, "", [], {}):
            return value
    return None


def _to_float(value: Any) -> float | None:
    """Convierte ``"1.250,50 €"`` en ``1250.5`` tolerando separadores ES/EN.

    Solo se toma el primer token numérico, de modo que sufijos como ``"80 m2"`` o
    ``"85,5 m²"`` no contaminan el resultado.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)

    match = _NUMBER_TOKEN.search(str(value))
    if match is None:
        return None
    text = match.group(0).rstrip(".,")
    if not text:
        return None

    has_comma, has_dot = "," in text, "." in text
    if has_comma and has_dot:
        if text.rfind(",") > text.rfind("."):
            text = text.replace(".", "").replace(",", ".")  # 1.250,50
        else:
            text = text.replace(",", "")  # 1,250.50
    elif has_comma:
        # Un único grupo de 3 dígitos finales se interpreta como separador de miles.
        text = text.replace(",", "") if len(text.split(",")[-1]) == 3 else text.replace(",", ".")
    elif has_dot:
        text = text.replace(".", "") if len(text.split(".")[-1]) == 3 else text

    try:
        return float(text)
    except ValueError:
        return None


def _to_int(value: Any) -> int | None:
    number = _to_float(value)
    return int(number) if number is not None else None


def _default_client_factory(token: str) -> Any:
    """Import perezoso del SDK: el módulo se puede importar sin apify instalado."""
    try:
        from apify_client import ApifyClient
    except ImportError as exc:  # pragma: no cover - depende del entorno
        raise ScraperException(
            "El SDK de Apify no está instalado (apify-client)",
            details={"missing_package": "apify-client"},
        ) from exc
    return ApifyClient(token)


class ApifyListingScraper:
    """Scraper multi-portal sobre actores de Apify."""

    def __init__(
        self,
        *,
        token: str,
        actor_ids: Mapping[Portal, str],
        max_items: int = 40,
        timeout_seconds: int = 300,
        operation: str = "rent",
        client_factory: Callable[[str], Any] = _default_client_factory,
        logger: Any | None = None,
    ) -> None:
        self._token = token
        self._actor_ids = dict(actor_ids)
        self._max_items = max_items
        self._timeout_seconds = timeout_seconds
        self._operation = operation
        self._client_factory = client_factory
        self._logger = logger or get_logger("app.scraper")

    async def fetch_listings(self, criteria: SearchCriteria) -> list[Listing]:
        listings: list[Listing] = []
        seen_urls: set[str] = set()

        for portal in criteria.portals:
            items = await self._fetch_portal(portal, criteria)
            for item in items:
                listing = self._normalize(portal, item)
                if listing is None or listing.url in seen_urls:
                    continue
                if not self._matches_hard_criteria(listing, criteria):
                    continue
                seen_urls.add(listing.url)
                listings.append(listing)

        return listings[: criteria.max_results]

    async def _fetch_portal(self, portal: Portal, criteria: SearchCriteria) -> list[dict[str, Any]]:
        actor_id = (self._actor_ids.get(portal) or "").strip()
        if not actor_id:
            raise ScraperException(
                f"No hay actor de Apify configurado para el portal {portal.value}",
                details={"portal": portal.value},
            )

        run_input = self._build_run_input(criteria)
        started = time.perf_counter()
        try:
            items = await asyncio.to_thread(self._run_actor, actor_id, run_input)
        except ScraperException:
            raise
        except Exception as exc:  # errores de red/SDK del proveedor
            duration_ms = round((time.perf_counter() - started) * 1000, 2)
            self._logger.warning(
                "search_scraper_failed",
                portal=portal.value,
                actor_id=actor_id,
                duration_ms=duration_ms,
                error_code=ScraperException.code,
                error_type=type(exc).__name__,
            )
            raise ScraperException(
                f"Fallo al scrapear el portal {portal.value}",
                details={"portal": portal.value, "actor_id": actor_id},
            ) from exc

        duration_ms = round((time.perf_counter() - started) * 1000, 2)
        self._logger.info(
            "listings_scraped",
            portal=portal.value,
            actor_id=actor_id,
            scraped_count=len(items),
            duration_ms=duration_ms,
        )
        return items

    def _build_run_input(self, criteria: SearchCriteria) -> dict[str, Any]:
        run_input: dict[str, Any] = {
            "location": criteria.location,
            "operation": self._operation,
            "maxItems": min(criteria.max_results, self._max_items),
        }
        optional = {
            "minPrice": criteria.min_price_eur,
            "maxPrice": criteria.max_price_eur,
            "minSize": criteria.min_size_m2,
            "minRooms": criteria.min_rooms,
        }
        run_input.update({key: value for key, value in optional.items() if value is not None})
        return run_input

    def _run_actor(self, actor_id: str, run_input: dict[str, Any]) -> list[dict[str, Any]]:
        """Ejecución bloqueante del actor (se invoca dentro de un hilo)."""
        client = self._client_factory(self._token)
        run = client.actor(actor_id).call(run_input=run_input, timeout_secs=self._timeout_seconds)
        if not run:
            raise ScraperException(
                "El actor de Apify no devolvió ninguna ejecución",
                details={"actor_id": actor_id},
            )

        dataset_id = run.get("defaultDatasetId") if isinstance(run, Mapping) else None
        if not dataset_id:
            raise ScraperException(
                "La ejecución de Apify no expone un dataset de resultados",
                details={"actor_id": actor_id},
            )

        items: list[dict[str, Any]] = []
        for item in client.dataset(dataset_id).iterate_items():
            if isinstance(item, Mapping):
                items.append(dict(item))
        return items

    def _normalize(self, portal: Portal, item: Mapping[str, Any]) -> Listing | None:
        """Traduce un item crudo del portal a la entidad de dominio ``Listing``."""
        url = _first_value(item, _FIELD_ALIASES["url"])
        price = _to_float(_first_value(item, _FIELD_ALIASES["price"]))
        if not isinstance(url, str) or not url.strip() or price is None or price <= 0:
            return None

        title = _first_value(item, _FIELD_ALIASES["title"])
        location = _first_value(item, _FIELD_ALIASES["location"])
        description = _first_value(item, _FIELD_ALIASES["description"])

        return Listing(
            portal=portal,
            url=url.strip(),
            title=str(title).strip() if title else "Vivienda sin título",
            price_eur=price,
            location=str(location).strip() if location else "",
            size_m2=_to_float(_first_value(item, _FIELD_ALIASES["size"])),
            rooms=_to_int(_first_value(item, _FIELD_ALIASES["rooms"])),
            bathrooms=_to_int(_first_value(item, _FIELD_ALIASES["bathrooms"])),
            description=str(description).strip() if description else None,
            raw=dict(item),
        )

    @staticmethod
    def _matches_hard_criteria(listing: Listing, criteria: SearchCriteria) -> bool:
        """Filtro barato en origen; el clasificador LLM hace el filtro fino."""
        if criteria.min_price_eur is not None and listing.price_eur < criteria.min_price_eur:
            return False
        if criteria.max_price_eur is not None and listing.price_eur > criteria.max_price_eur:
            return False
        if criteria.min_size_m2 is not None:
            if listing.size_m2 is None or listing.size_m2 < criteria.min_size_m2:
                return False
        if criteria.min_rooms is not None:
            if listing.rooms is None or listing.rooms < criteria.min_rooms:
                return False
        return True
