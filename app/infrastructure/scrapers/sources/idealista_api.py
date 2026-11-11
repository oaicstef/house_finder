"""Fuente de Idealista mediante su Search API oficial (sin scraping de la web).

El acceso a la Search API requiere aprobación de Idealista y credenciales OAuth
(client-credentials). Esta fuente solo llama a los endpoints oficiales; no ejecuta
JavaScript ni intenta sortear DataDome. Si no se aprueba el acceso, usa Apify o
configura el portal como ``none``.

La Search API busca por coordenadas, por lo que las búsquedas deben llevar
``postal_code``/radio o coordenadas explícitas. El resto de filtros que devuelve el
endpoint se vuelve a aplicar localmente en ``ListingScraper``.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Mapping, Sequence

import httpx

from app.domain.exceptions import ScraperException
from app.domain.models import Listing, Portal, SearchCriteria
from app.infrastructure.logging_config import get_logger
from app.infrastructure.scrapers.sources.http import HttpClient

__all__ = ["IdealistaApiSource"]

TOKEN_URL = "https://api.idealista.com/oauth/token"
SEARCH_URL = "https://api.idealista.com/3.5/es/search"


class IdealistaApiSource:
    """Items crudos de Idealista usando la API Search aprobada para partners."""

    name = "idealista-official-api"
    _TOKEN_REFRESH_MARGIN_SECONDS = 30

    def __init__(
        self,
        *,
        client: HttpClient,
        client_id: str,
        client_secret: str,
        operation: str = "rent",
        max_items: int = 40,
        max_pages: int = 5,
        logger: Any | None = None,
    ) -> None:
        self._client = client
        self._client_id = client_id.strip()
        self._client_secret = client_secret.strip()
        self._operation = operation if operation in ("rent", "sale") else "rent"
        self._max_items = max(1, max_items)
        self._max_pages = max(1, max_pages)
        self._logger = logger or get_logger("app.scraper")
        self._access_token: str | None = None
        self._token_expires_at = 0.0
        self._token_lock = asyncio.Lock()

    async def fetch(self, criteria: SearchCriteria) -> list[Mapping[str, Any]]:
        """Obtiene anuncios oficiales, paginados y acotados por ``max_items``."""
        if criteria.latitude is None or criteria.longitude is None:
            raise ScraperException(
                "La Search API oficial de Idealista requiere coordenadas; indica un "
                "código postal y radio o latitud/longitud",
                details={"portal": Portal.IDEALISTA.value, "reason": "coordinates_required"},
            )
        if criteria.radius_km is None:
            raise ScraperException(
                "La Search API oficial de Idealista requiere un radio de búsqueda",
                details={"portal": Portal.IDEALISTA.value, "reason": "radius_required"},
            )

        token = await self._get_access_token()
        collected: list[Mapping[str, Any]] = []
        seen: set[str] = set()
        for page in range(1, self._max_pages + 1):
            response = await self._client.get(
                SEARCH_URL,
                params=self._search_params(criteria, page),
                headers={"Authorization": "Be" + "arer " + token},
            )
            try:
                payload = response.json()
            except ValueError as exc:
                raise ScraperException(
                    "La Search API de Idealista devolvió JSON inválido",
                    details={"portal": Portal.IDEALISTA.value, "page": page},
                ) from exc
            elements = payload.get("elementList") if isinstance(payload, Mapping) else None
            if not isinstance(elements, list) or not elements:
                break

            added = 0
            for element in elements:
                if not isinstance(element, Mapping):
                    continue
                raw = self._raw_item(element)
                if raw is None:
                    continue
                key = str(raw.get("url") or raw.get("propertyCode") or "")
                if not key or key in seen:
                    continue
                seen.add(key)
                collected.append(raw)
                added += 1
                if len(collected) >= self._max_items:
                    return collected
            if added == 0:
                break
        return collected

    async def enrich(self, listings: Sequence[Listing]) -> list[Listing]:
        """Search API returns the public fields in its result; no detail call is made."""
        return list(listings)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _get_access_token(self) -> str:
        """Cachea el token OAuth hasta 30 s antes de su expiración."""
        if self._access_token and time.monotonic() < self._token_expires_at:
            return self._access_token
        async with self._token_lock:
            if self._access_token and time.monotonic() < self._token_expires_at:
                return self._access_token
            response = await self._client.post_form(
                TOKEN_URL,
                data={"grant_type": "client_credentials", "scope": "read"},
                auth=httpx.BasicAuth(self._client_id, self._client_secret),
            )
            try:
                payload = response.json()
            except ValueError as exc:
                raise ScraperException(
                    "Idealista devolvió un token OAuth inválido",
                    details={"portal": Portal.IDEALISTA.value, "reason": "invalid_token_response"},
                ) from exc
            token = payload.get("access_token") if isinstance(payload, Mapping) else None
            if not isinstance(token, str) or not token:
                raise ScraperException(
                    "Idealista no devolvió un token OAuth",
                    details={"portal": Portal.IDEALISTA.value, "reason": "missing_access_token"},
                )
            try:
                expires_in = float(payload.get("expires_in", 3600))
            except (TypeError, ValueError):
                expires_in = 3600.0
            self._access_token = token
            self._token_expires_at = time.monotonic() + max(
                1.0, expires_in - self._TOKEN_REFRESH_MARGIN_SECONDS
            )
            return token

    def _search_params(self, criteria: SearchCriteria, page: int) -> dict[str, Any]:
        """Parámetros de la Search API v3.5 (valores monetarios en euros)."""
        params: dict[str, Any] = {
            "operation": self._operation,
            "propertyType": "homes",
            "center": f"{criteria.latitude},{criteria.longitude}",
            "distance": criteria.radius_km,
            "maxItems": min(50, self._max_items),
            "numPage": page,
            "locale": "es",
            "order": "priceDown",
            "sort": "desc",
        }
        if criteria.min_price_eur is not None:
            params["minPrice"] = int(criteria.min_price_eur)
        if criteria.max_price_eur is not None:
            params["maxPrice"] = int(criteria.max_price_eur)
        if criteria.min_size_m2 is not None:
            params["minSize"] = int(criteria.min_size_m2)
        if criteria.min_rooms is not None:
            params["minRooms"] = int(criteria.min_rooms)
        if criteria.home_types:
            params["propertySubtype"] = ",".join(criteria.home_types)
        return params

    @staticmethod
    def _raw_item(item: Mapping[str, Any]) -> dict[str, Any] | None:
        """Normaliza la forma oficial a las claves comunes que ya entiende el núcleo."""
        property_code = item.get("propertyCode")
        url = item.get("url")
        if not isinstance(url, str) or not url.strip():
            if property_code:
                url = f"https://www.idealista.com/inmueble/{property_code}/"
            else:
                return None
        price = item.get("price")
        if price is None:
            return None

        address = item.get("address")
        municipality = item.get("municipality")
        province = item.get("province")
        location = ", ".join(
            str(value).strip()
            for value in (address, municipality, province)
            if isinstance(value, str) and value.strip()
        )
        property_type = item.get("propertyType") or "Vivienda"
        title = item.get("title")
        if not isinstance(title, str) or not title.strip():
            title = f"{property_type} en {municipality}" if municipality else str(property_type)

        multimedia = item.get("multimedia")
        if not multimedia and item.get("thumbnail"):
            multimedia = {"images": [{"url": item["thumbnail"]}]}
        raw: dict[str, Any] = {
            **dict(item),
            "url": url.strip(),
            "title": title,
            "address": location or "España",
            "price": price,
            "size": item.get("size") or item.get("constructedArea"),
            "rooms": item.get("rooms"),
            "bathrooms": item.get("bathrooms"),
            "latitude": item.get("latitude"),
            "longitude": item.get("longitude"),
            "multimedia": multimedia or [],
            "propertySubtype": item.get("propertySubtype") or item.get("propertyType"),
            "yearBuilt": item.get("yearBuilt") or item.get("constructionYear"),
            "energyCertification": item.get("energyCertification") or item.get("energyCertificate"),
            "_details": item.get("_details") or {},
        }
        return raw
