"""Adaptador de scraping basado en Apify.

Implementa ``ScraperPort`` delegando en un actor de Apify por portal. El cliente de
Apify es síncrono, por lo que cada ejecución se aísla con ``asyncio.to_thread`` para
no bloquear el event loop de FastAPI.
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from typing import Any, Callable, Mapping, Sequence

from app.domain.exceptions import ScraperException
from app.domain.models import AMENITY_LABELS, Listing, Portal, SearchCriteria, normalize_text
from app.infrastructure.logging_config import get_logger

__all__ = ["ApifyListingScraper"]

#: Estados terminales de un run de Apify que implican que no hay datos utilizables.
_FAILED_RUN_STATUSES = frozenset({"FAILED", "ABORTED", "TIMED-OUT"})

#: Claves candidatas en el item crudo del actor -> campo normalizado.
#: Una clave con puntos (``transaction.price``) se resuelve como ruta anidada.
_FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "url": ("url", "link", "propertyUrl", "detailUrl", "href", "property_url"),
    "title": ("title", "name", "propertyTitle", "headline"),
    "price": (
        "price",
        "transaction.price",
        "priceEur",
        "priceAmount",
        "priceValue",
        "currentPrice",
    ),
    "size": ("size", "squareMeters", "surface", "surfaceArea", "area", "constructedArea"),
    "rooms": ("rooms", "bedrooms", "roomCount"),
    "bathrooms": ("bathrooms", "baths", "bathroomCount"),
    "location": ("address", "location", "city", "district", "place", "neighborhood"),
    "description": ("description", "body", "summary", "details"),
}

#: Comodidades que aportan los actores -> nombre canónico en el dominio.
_AMENITY_FLAGS: dict[str, str] = {
    "hasSwimmingPool": "swimming_pool",
    "hasGarden": "garden",
    "hasTerrace": "terrace",
    "hasAirConditioning": "air_conditioning",
    "hasLift": "lift",
    "hasBoxRoom": "storage_room",
}


@dataclass(frozen=True, slots=True)
class _PortalProfile:
    """Traduce los conceptos del dominio al vocabulario de cada actor de Apify.

    Los actores de Apify Store no comparten esquema ni en la entrada ni en la salida:
    Idealista usa ``sale``/``homes`` con precios en string y devuelve título; Fotocasa
    usa ``buy``/``home`` con enteros, no publica título y anida el precio en
    ``transaction.price``. Los *builders* cubren esa variación de salida.
    """

    operation_map: Mapping[str, str]
    property_type: str
    country: str | None
    price_as_string: bool
    #: Compone el título cuando el actor no lo devuelve.
    title_builder: Callable[[Mapping[str, Any]], str | None] | None = None
    #: Compone la ubicación cuando el actor devuelve un objeto en vez de texto.
    location_builder: Callable[[Mapping[str, Any]], str | None] | None = None


_NUMBER_TOKEN = re.compile(r"-?\d[\d.,]*")


def _first_value(item: Mapping[str, Any], keys: Sequence[str]) -> Any | None:
    """Primer valor utilizable de ``keys``; admite rutas anidadas con puntos."""
    for key in keys:
        value: Any = item
        for part in key.split("."):
            if not isinstance(value, Mapping):
                value = None
                break
            value = value.get(part)
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


def _suggested_title(item: Mapping[str, Any]) -> str | None:
    """Algunos actores anidan el título en ``suggestedTexts.title``."""
    texts = item.get("suggestedTexts")
    if isinstance(texts, Mapping):
        title = texts.get("title")
        if isinstance(title, str) and title.strip():
            return title.strip()
    return None


def _fotocasa_location(item: Mapping[str, Any]) -> str | None:
    """``Ventas, Ciudad Lineal, Madrid`` a partir de los niveles del portal.

    El ``location`` de Fotocasa es un objeto de ids y nombres; serializarlo tal cual
    dejaba el campo del dominio lleno de ids (``{'level1Id': '14', ...}``).
    """
    location = item.get("location")
    if not isinstance(location, Mapping):
        return None

    parts: list[str] = []
    for key in ("level8Name", "level7Name", "level5Name", "level2Name"):
        name = location.get(key)
        if isinstance(name, str) and name.strip() and name.strip() not in parts:
            parts.append(name.strip())
    return ", ".join(parts) or None


def _text(value: Any) -> str | None:
    """Texto limpio de un valor escalar, o ``None`` si no aporta nada."""
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    text = str(value).strip()
    return text or None


def _fotocasa_title(item: Mapping[str, Any]) -> str | None:
    """Compone ``Calle Montejurra 33, Ventas, Ciudad Lineal, Madrid``.

    El actor no publica un campo de título: se arma con la dirección y la ubicación.
    """
    address = " ".join(
        part for part in (_text(item.get("street")), _text(item.get("number"))) if part
    )
    zone = _fotocasa_location(item)
    if address and zone:
        return f"{address}, {zone}"
    return address or zone


_PROFILES: dict[Portal, _PortalProfile] = {
    Portal.IDEALISTA: _PortalProfile(
        # Verificado en vivo contra igolaizola/idealista-scraper.
        operation_map={"rent": "rent", "sale": "sale"},
        property_type="homes",
        country="es",
        price_as_string=True,
    ),
    Portal.FOTOCASA: _PortalProfile(
        # Verificado en vivo contra igolaizola/fotocasa-scraper (build zQ6P2E7A5hwEubfcJ).
        operation_map={"rent": "rent", "sale": "buy"},
        property_type="home",
        country=None,
        price_as_string=False,
        title_builder=_fotocasa_title,
        location_builder=_fotocasa_location,
    ),
}


def _extract_amenities(item: Mapping[str, Any]) -> tuple[str, ...]:
    """Traduce los flags del portal a comodidades canónicas en español-legible."""
    keys: dict[str, Any] = {}
    features = item.get("features")
    if isinstance(features, Mapping):
        keys.update(features)
    # Algunos actores ponen los flags en la raíz del item.
    for flag in _AMENITY_FLAGS:
        if flag in item:
            keys.setdefault(flag, item[flag])

    amenities: list[str] = []
    for flag, name in _AMENITY_FLAGS.items():
        if keys.get(flag) is True or str(keys.get(flag)).strip().lower() == "true":
            amenities.append(name)
    return tuple(amenities)


def _as_int_string(value: float) -> str:
    """``1400.0`` -> ``"1400"`` (los actores esperan strings sin decimales)."""
    return str(int(value)) if float(value).is_integer() else str(value)


def _url_amenities(url: str) -> tuple[str, ...]:
    """Comodidades deducidas del slug del anuncio.

    Fotocasa no publica etiquetas legibles en ``features`` (solo ids) pero enumera las
    comodidades en la URL: ``…/aire-acondicionado-jardin-terraza-piscina/190860387/d``.
    Se reutilizan los sinónimos del dominio (``AMENITY_LABELS``) para no duplicar
    vocabulario; en Idealista la URL es un id numérico y no coincide con nada.
    """
    text = normalize_text(url.replace("-", " ").replace("/", " ").replace("_", " "))
    return tuple(
        amenity
        for amenity, labels in AMENITY_LABELS.items()
        if any(label in text for label in labels)
    )


def _dataset_id(run: Any) -> str | None:
    """Extrae el id del dataset soportando el modelo ``Run`` de v3 y diccionarios."""
    value = getattr(run, "default_dataset_id", None)
    if value:
        return str(value)
    if isinstance(run, Mapping):
        for key in ("defaultDatasetId", "default_dataset_id"):
            value = run.get(key)
            if value:
                return str(value)
    return None


def _run_status(run: Any) -> str | None:
    """Estado del run (``None`` si la implementación no lo expone)."""
    status = getattr(run, "status", None)
    if status is None and isinstance(run, Mapping):
        status = run.get("status")
    return str(status) if status is not None else None


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
        max_charge_usd: float | None = None,
        client_factory: Callable[[str], Any] = _default_client_factory,
        logger: Any | None = None,
    ) -> None:
        self._token = token
        self._actor_ids = dict(actor_ids)
        self._max_items = max_items
        self._timeout_seconds = timeout_seconds
        self._operation = operation
        self._max_charge_usd = max_charge_usd
        self._client_factory = client_factory
        self._logger = logger or get_logger("app.scraper")

    async def fetch_listings(self, criteria: SearchCriteria) -> list[Listing]:
        listings: list[Listing] = []
        seen_urls: set[str] = set()
        skipped: list[str] = []

        for portal in criteria.portals:
            if not (self._actor_ids.get(portal) or "").strip():
                # Un portal sin actor configurado no debe tumbar la búsqueda entera.
                skipped.append(portal.value)
                self._logger.warning(
                    "scraper_portal_skipped",
                    portal=portal.value,
                    error_code=None,
                    reason="actor_not_configured",
                )
                continue

            items = await self._fetch_portal(portal, criteria)
            for item in items:
                listing = self._normalize(portal, item)
                if listing is None or listing.url in seen_urls:
                    continue
                if not self._matches_hard_criteria(listing, criteria):
                    continue
                seen_urls.add(listing.url)
                listings.append(listing)

        if skipped and len(skipped) == len(criteria.portals):
            raise ScraperException(
                "Ningún portal tiene un actor de Apify configurado",
                details={"portals": skipped},
            )

        # Se devuelven TODOS los candidatos (acotados por ``max_items``): recortar aquí
        # a ``max_results`` impediría que los filtros posteriores (must_have) y el
        # clasificador vieran más opciones que las estrictamente devueltas al cliente.
        return listings

    async def _fetch_portal(self, portal: Portal, criteria: SearchCriteria) -> list[dict[str, Any]]:
        actor_id = (self._actor_ids.get(portal) or "").strip()
        if not actor_id:
            raise ScraperException(
                f"No hay actor de Apify configurado para el portal {portal.value}",
                details={"portal": portal.value},
            )

        run_input = self._build_run_input(criteria, portal)
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

    def _build_run_input(self, criteria: SearchCriteria, portal: Portal) -> dict[str, Any]:
        """Construye el input del actor según el vocabulario de cada portal.

        ``min_rooms`` no se envía: no todos los actores lo soportan y una clave
        desconocida hace fallar la validación del input. Se filtra en
        ``_matches_hard_criteria``.
        """
        profile = _PROFILES[portal]
        operation = profile.operation_map.get(self._operation, self._operation)
        # Se piden candidatos hasta APIFY_MAX_ITEMS, no solo `max_results`: si se
        # pidieran 5 anuncios para devolver 5, cualquier filtro posterior dejaría la
        # búsqueda vacía. `max_results` solo recorta la respuesta final.
        total_items = self._max_items

        run_input: dict[str, Any] = {
            "operation": operation,
            "propertyType": profile.property_type,
            "location": criteria.location,
            "maxItems": total_items,
        }
        if profile.country:
            run_input["country"] = profile.country

        def price(value: float) -> Any:
            return _as_int_string(value) if profile.price_as_string else int(value)

        if criteria.min_price_eur is not None:
            run_input["minPrice"] = price(criteria.min_price_eur)
        if criteria.max_price_eur is not None:
            run_input["maxPrice"] = price(criteria.max_price_eur)
        if criteria.min_size_m2 is not None:
            run_input["minSize"] = (
                _as_int_string(criteria.min_size_m2)
                if profile.price_as_string
                else int(criteria.min_size_m2)
            )
        return run_input

    def _run_actor(self, actor_id: str, run_input: dict[str, Any]) -> list[dict[str, Any]]:
        """Ejecución bloqueante del actor (se invoca dentro de un hilo).

        Usa la firma de ``apify-client`` v3: ``run_timeout`` (duración máxima del run)
        y ``max_total_charge_usd`` (tope de gasto por ejecución).
        """
        client = self._client_factory(self._token)

        call_kwargs: dict[str, Any] = {
            "run_input": run_input,
            "run_timeout": timedelta(seconds=self._timeout_seconds),
        }
        if self._max_charge_usd is not None:
            call_kwargs["max_total_charge_usd"] = Decimal(str(self._max_charge_usd))

        run = client.actor(actor_id).call(**call_kwargs)
        if run is None:
            raise ScraperException(
                "El actor de Apify no devolvió ninguna ejecución",
                details={"actor_id": actor_id},
            )

        status = _run_status(run)
        if status in _FAILED_RUN_STATUSES:
            raise ScraperException(
                f"La ejecución de Apify terminó en estado {status}",
                details={"actor_id": actor_id, "status": status},
            )

        dataset_id = _dataset_id(run)
        if not dataset_id:
            raise ScraperException(
                "La ejecución de Apify no expone un dataset de resultados",
                details={"actor_id": actor_id, "status": status},
            )

        items: list[dict[str, Any]] = []
        for item in client.dataset(dataset_id).iterate_items():
            if isinstance(item, Mapping):
                items.append(dict(item))
        return items

    def _normalize(self, portal: Portal, item: Mapping[str, Any]) -> Listing | None:
        """Traduce un item crudo del portal a la entidad de dominio ``Listing``."""
        profile = _PROFILES[portal]
        url = _first_value(item, _FIELD_ALIASES["url"])
        price = _to_float(_first_value(item, _FIELD_ALIASES["price"]))
        if not isinstance(url, str) or not url.strip() or price is None or price <= 0:
            return None

        title = _first_value(item, _FIELD_ALIASES["title"]) or _suggested_title(item)
        if not isinstance(title, str) or not title.strip():
            title = profile.title_builder(item) if profile.title_builder is not None else None

        location = _first_value(item, _FIELD_ALIASES["location"])
        if not isinstance(location, str) and profile.location_builder is not None:
            location = profile.location_builder(item)
        description = _first_value(item, _FIELD_ALIASES["description"])
        amenities = _extract_amenities(item) + _url_amenities(url.strip())

        return Listing(
            portal=portal,
            url=url.strip(),
            title=title.strip() if isinstance(title, str) and title.strip() else "Vivienda sin título",
            price_eur=price,
            location=location.strip() if isinstance(location, str) else "",
            size_m2=_to_float(_first_value(item, _FIELD_ALIASES["size"])),
            rooms=_to_int(_first_value(item, _FIELD_ALIASES["rooms"])),
            bathrooms=_to_int(_first_value(item, _FIELD_ALIASES["bathrooms"])),
            description=str(description).strip() if description else None,
            amenities=tuple(dict.fromkeys(amenities)),
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
