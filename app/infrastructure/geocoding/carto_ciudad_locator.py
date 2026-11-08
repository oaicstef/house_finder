"""Adaptador de geocodificación de códigos postales contra CartoCiudad.

CartoCiudad es un servicio público del IGN/CNIG que resuelve códigos postales
españoles sin API key. El cliente HTTP es la stdlib y se ejecuta en un hilo para no
bloquear el event loop; los resultados se cachean en memoria por instancia.
"""

from __future__ import annotations

import asyncio
import json
import time
import urllib.parse
import urllib.request
from typing import Any, Callable, Mapping

from app.domain.exceptions import GeoLocationException
from app.domain.models import Coordinates
from app.infrastructure.logging_config import get_logger

__all__ = ["DEFAULT_BASE_URL", "CartoCiudadPostalCodeLocator"]

DEFAULT_BASE_URL = "https://www.cartociudad.es/geocoder/api/geocoder"
_USER_AGENT = "house-finder/1.0 (+https://github.com/oaicstef/house_finder)"


class CartoCiudadPostalCodeLocator:
    """Resuelve un CP de 5 dígitos en su centroide según CartoCiudad."""

    def __init__(
        self,
        *,
        base_url: str = DEFAULT_BASE_URL,
        timeout_seconds: float = 10.0,
        http_get: Callable[[str], Any] | None = None,
        logger: Any | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds
        self._http_get = http_get or self._default_http_get
        self._logger = logger or get_logger("app.geocoding")
        self._cache: dict[str, Coordinates | None] = {}

    async def locate(self, postal_code: str) -> Coordinates | None:
        """Devuelve el centroide del CP o ``None`` si no existe."""
        code = (postal_code or "").strip()
        if code in self._cache:
            return self._cache[code]

        try:
            coordinates = await asyncio.to_thread(self._fetch, code)
        except GeoLocationException as exc:
            self._logger.warning(
                "geo_location_failed",
                postal_code=code,
                error_code=exc.code,
                details=exc.details,
            )
            raise

        self._cache[code] = coordinates
        return coordinates

    def _fetch(self, postal_code: str) -> Coordinates | None:
        query = urllib.parse.urlencode(
            {"q": postal_code, "type": "postalcode", "limit": 1}
        )
        url = f"{self._base_url}/find?{query}"
        started = time.perf_counter()
        try:
            payload = self._http_get(url)
        except Exception as exc:  # red/SDK del proveedor
            duration_ms = self._elapsed_ms(started)
            raise GeoLocationException(
                "Fallo al consultar el geocodificador",
                details={
                    "postal_code": postal_code,
                    "duration_ms": duration_ms,
                    "error_type": type(exc).__name__,
                },
            ) from exc

        item = _first_result(payload)
        if item is None:
            return None

        try:
            return Coordinates(
                latitude=float(item["lat"]),
                longitude=float(item["lng"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise GeoLocationException(
                "El geocodificador devolvió coordenadas inválidas",
                details={"postal_code": postal_code},
            ) from exc

    def _default_http_get(self, url: str) -> Any:
        request = urllib.request.Request(
            url,
            headers={"User-Agent": _USER_AGENT, "Accept": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=self._timeout_seconds) as response:
            return json.loads(response.read().decode("utf-8"))

    @staticmethod
    def _elapsed_ms(started: float) -> float:
        return round((time.perf_counter() - started) * 1000, 2)


def _first_result(payload: Any) -> Mapping[str, Any] | None:
    """Normaliza la respuesta de CartoCiudad (objeto único o lista)."""
    if isinstance(payload, Mapping):
        items: list[Any] = [payload]
    elif isinstance(payload, list):
        items = payload
    else:
        return None

    for item in items:
        if not isinstance(item, Mapping):
            continue
        if item.get("lat") is None or item.get("lng") is None:
            continue
        return item
    return None
