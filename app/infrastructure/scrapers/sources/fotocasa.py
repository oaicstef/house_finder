"""Fuente de anuncios de Fotocasa mediante HTTP directo.

Fotocasa renderiza la búsqueda en el servidor y publica el resultado completo en
``<script type="application/json" id="__initial_props__">``; no hace falta navegador
ni API privada. Los nombres de los parámetros (``maxPrice``, ``minSurface``,
``minRooms``, ``page``) se verificaron en vivo contra el portal.

Los items se devuelven en el mismo esquema que consumía el actor de Apify, de modo
que ``normalization`` y los filtros del dominio funcionan sin cambios.
"""

from __future__ import annotations

import json
import re
import time
import unicodedata
from typing import Any, Mapping, Sequence

from app.domain.exceptions import ScraperException
from app.domain.models import Listing, Portal, SearchCriteria
from app.infrastructure.logging_config import get_logger
from app.infrastructure.scrapers.sources.http import HttpClient

__all__ = ["FotocasaHttpSource", "location_segment"]

BASE_URL = "https://www.fotocasa.es"
#: Segmento de la URL por operación (``/es/comprar/...`` y ``/es/alquiler/...``).
_OPERATION_PATH = {"rent": "alquiler", "sale": "comprar"}
#: Página de búsqueda: ``<script id="__initial_props__">`` con el JSON completo.
_INITIAL_PROPS = re.compile(
    r'<script[^>]*id="__initial_props__"[^>]*>(?P<payload>.*?)</script>',
    re.DOTALL,
)
#: Capitales y ciudades frecuentes -> segmento que usa Fotocasa.
_CITY_SEGMENTS = {
    "madrid": "madrid-capital",
    "barcelona": "barcelona-capital",
    "valencia": "valencia-capital",
    "sevilla": "sevilla-capital",
    "zaragoza": "zaragoza-capital",
    "malaga": "malaga-capital",
    "murcia": "murcia-capital",
    "granada": "granada-capital",
    "valladolid": "valladolid-capital",
    "alicante": "alicante-alacant",
    "palma": "palma-de-mallorca",
    "las palmas de gran canaria": "las-palmas-de-gran-canaria",
    "bilbao": "bilbao",
    "vigo": "vigo",
    "gijon": "gijon",
}
#: ``features`` de Fotocasa -> clave booleana que entiende la normalización.
_AMENITY_FEATURES = {
    "terrace": "hasTerrace",
    "elevator": "hasLift",
    "air_conditioner": "hasAirConditioning",
    "garden": "hasGarden",
    "pool": "hasSwimmingPool",
    "swimming_pool": "hasSwimmingPool",
    "storage_room": "hasBoxRoom",
    "trastero": "hasBoxRoom",
}


def _slug(text: str) -> str:
    """``Málaga Capital`` -> ``malaga-capital``."""
    decomposed = unicodedata.normalize("NFKD", text)
    ascii_text = "".join(char for char in decomposed if not unicodedata.combining(char))
    cleaned = re.sub(r"[^a-z0-9]+", "-", ascii_text.strip().lower()).strip("-")
    return cleaned


def location_segment(location: str, override: str | None = None) -> str:
    """Segmento de ubicación de Fotocasa a partir del texto libre de los criterios.

    El perfil guarda ``"Ventas, Ciudad Lineal, Madrid"``: se toma el último nivel
    (la ciudad) y se busca a un segmento conocido. Buscar por ciudad y dejar que el
    filtro de radio recorte es preferible a fallar con un barrio que Fotocasa
    publica bajo otro id.
    """
    if override:
        return _slug(override)
    parts = [part.strip() for part in location.split(",") if part.strip()]
    if not parts:
        return "madrid-capital"
    city = parts[-1]
    return _CITY_SEGMENTS.get(_slug(city).replace("-", " "), _slug(city))


def _int_or_none(value: Any) -> int | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return int(number) if number else None


def _raw_item(item: Mapping[str, Any], operation: str) -> dict[str, Any] | None:
    """Traduce un ``realEstate`` de Fotocasa al item crudo que espera el núcleo."""
    detail = item.get("detail")
    path = detail.get("es-ES") if isinstance(detail, Mapping) else None
    if not isinstance(path, str) or not path:
        detail_with_params = item.get("detailWithParams")
        path = detail_with_params.get("es-ES") if isinstance(detail_with_params, Mapping) else None
    price = _int_or_none(item.get("rawPrice"))
    if not isinstance(path, str) or not path or price is None or price <= 0:
        return None

    features = {
        str(feature.get("key")): feature.get("value")
        for feature in item.get("features") or ()
        if isinstance(feature, Mapping) and feature.get("key")
    }
    address = item.get("address") if isinstance(item.get("address"), Mapping) else {}
    coordinates = item.get("coordinates") if isinstance(item.get("coordinates"), Mapping) else {}

    raw: dict[str, Any] = {
        "propertyId": str(item.get("id") or ""),
        "url": f"{BASE_URL}{path}",
        # El precio va anidado como en el actor de Apify (``transaction.price``).
        "transaction": {"type": operation, "price": price},
        "price": price,
        "description": item.get("description"),
        "multimedia": item.get("multimedia") or [],
        "features": item.get("features") or [],
        "dynamicFeatures": item.get("dynamicFeatures") or [],
        "propertyType": item.get("buildingType"),
        "detailedType": {"subTypology": item.get("buildingSubtype")},
        "location": {
            "level8Name": address.get("neighborhood"),
            "level7Name": address.get("district"),
            "level5Name": address.get("city"),
            "level2Name": address.get("province"),
        },
        "latitude": coordinates.get("latitude"),
        "longitude": coordinates.get("longitude"),
        "surface": features.get("surface"),
        "rooms": features.get("rooms"),
        "baths": features.get("bathrooms"),
        # Fotocasa publica la antigüedad en años, no el año de construcción.
        "antiquity": features.get("antiquity"),
    }
    for key in (
        "newDevelopment",
        "isNewDevelopment",
        "newConstruction",
        "isNewConstruction",
        "isNewBuild",
        "isNew",
        "new_development",
        "is_new",
        "listingCondition",
        "propertyCondition",
        "condition",
    ):
        if key in item:
            raw[key] = item[key]
    for feature, flag in _AMENITY_FEATURES.items():
        if feature in features:
            raw[flag] = True
    return raw


class FotocasaHttpSource:
    """Items crudos de Fotocasa (búsqueda SSR + JSON embebido)."""

    name = "fotocasa-http"

    def __init__(
        self,
        *,
        client: HttpClient,
        operation: str = "rent",
        max_items: int = 40,
        max_pages: int = 5,
        location_override: str | None = None,
        logger: Any | None = None,
    ) -> None:
        self._client = client
        self._operation = operation if operation in _OPERATION_PATH else "rent"
        self._max_items = max(1, max_items)
        self._max_pages = max(1, max_pages)
        self._location_override = location_override
        self._logger = logger or get_logger("app.scraper")

    async def fetch(self, criteria: SearchCriteria) -> list[Mapping[str, Any]]:
        """Anuncios crudos de la búsqueda paginada."""
        url = self._search_url(criteria)
        collected: list[Mapping[str, Any]] = []
        seen: set[str] = set()

        for page in range(1, self._max_pages + 1):
            items = await self._search_page(url, criteria, page)
            if not items:
                break
            added = 0
            for item in items:
                raw = _raw_item(item, self._operation)
                if raw is None:
                    continue
                key = raw["propertyId"] or raw["url"]
                if key in seen:
                    continue
                seen.add(key)
                collected.append(raw)
                added += 1
                if len(collected) >= self._max_items:
                    return collected
            if added == 0:
                # Página repetida o solo anuncios descartables: no hay más que sacar.
                break
        return collected

    async def enrich(self, listings: Sequence[Listing]) -> list[Listing]:
        """Fotocasa no publica certificado ni descripciones alternativas."""
        return list(listings)

    async def aclose(self) -> None:
        await self._client.aclose()

    # ------------------------------------------------------------- helpers ---

    def _search_url(self, criteria: SearchCriteria) -> str:
        path = _OPERATION_PATH[self._operation]
        segment = location_segment(criteria.location, self._location_override)
        return f"{BASE_URL}/es/{path}/viviendas/{segment}/todas-las-zonas/l"

    @staticmethod
    def _search_params(criteria: SearchCriteria, page: int) -> dict[str, Any]:
        """Filtros que el portal acepta en la URL (verificados en vivo)."""
        params: dict[str, Any] = {"page": page}
        if criteria.max_price_eur is not None:
            params["maxPrice"] = int(round(criteria.max_price_eur))
        if criteria.min_price_eur is not None:
            params["minPrice"] = int(round(criteria.min_price_eur))
        if criteria.min_size_m2 is not None:
            params["minSurface"] = int(round(criteria.min_size_m2))
        if criteria.min_rooms is not None:
            params["minRooms"] = int(criteria.min_rooms)
        if criteria.latitude is not None and criteria.longitude is not None:
            params["latitude"] = criteria.latitude
            params["longitude"] = criteria.longitude
        return params

    async def _search_page(
        self, url: str, criteria: SearchCriteria, page: int
    ) -> list[Mapping[str, Any]]:
        started = time.perf_counter()
        response = await self._client.get(url, params=self._search_params(criteria, page))
        items = self._parse(response.text, url)
        duration_ms = round((time.perf_counter() - started) * 1000, 2)
        if items:
            self._logger.info(
                "listings_scraped",
                portal=Portal.FOTOCASA.value,
                provider=self.name,
                scraped_count=len(items),
                page=page,
                location=criteria.location,
                duration_ms=duration_ms,
            )
        else:
            self._logger.warning(
                "scraper_portal_returned_no_listings",
                portal=Portal.FOTOCASA.value,
                provider=self.name,
                page=page,
                location=criteria.location,
                error_code=None,
                duration_ms=duration_ms,
            )
        return items

    @staticmethod
    def _parse(html: str, url: str) -> list[Mapping[str, Any]]:
        match = _INITIAL_PROPS.search(html)
        if match is None:
            raise ScraperException(
                "Fotocasa no devolvió los datos de la búsqueda",
                details={"url": url, "reason": "missing_initial_props"},
            )
        try:
            payload = json.loads(match.group("payload"))
        except json.JSONDecodeError as exc:
            raise ScraperException(
                "La respuesta de Fotocasa no es JSON válido",
                details={"url": url, "error": exc.msg},
            ) from exc
        result = (payload.get("initialSearch") or {}).get("result") or {}
        items = result.get("realEstates") or []
        return [item for item in items if isinstance(item, Mapping)]
