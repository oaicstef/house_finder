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
from datetime import date, timedelta
from decimal import Decimal
from typing import Any, Callable, Mapping, Sequence

from app.domain.exceptions import ScraperException
from app.domain.models import (
    AMENITY_LABELS,
    Coordinates,
    Listing,
    Portal,
    SearchCriteria,
    normalize_text,
)
from app.infrastructure.logging_config import get_logger

__all__ = ["ApifyListingScraper"]

#: Estados terminales de un run de Apify que implican que no hay datos utilizables.
_FAILED_RUN_STATUSES = frozenset({"FAILED", "ABORTED", "TIMED-OUT"})

#: Claves candidatas en el item crudo del actor -> campo normalizado.
_FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "url": ("url", "link", "propertyUrl", "detailUrl", "href", "property_url"),
    "title": ("title", "name", "propertyTitle", "headline"),
    "price": ("price", "priceEur", "priceAmount", "priceValue", "currentPrice"),
    "size": ("size", "squareMeters", "surface", "surfaceArea", "area", "constructedArea"),
    "rooms": ("rooms", "bedrooms", "roomCount"),
    "bathrooms": ("bathrooms", "baths", "bathroomCount"),
    "location": ("address", "location", "city", "district", "place", "neighborhood"),
    "description": ("description", "body", "summary", "details"),
}

#: Claves donde Idealista/Fotocasa pueden publicar el año de construcción.
_YEAR_ALIASES: tuple[str, ...] = (
    "yearBuilt",
    "year_built",
    "constructionYear",
    "yearConstruction",
    "builtYear",
    "construction_year",
)

#: Fotocasa publica la antigüedad en años, no el año de construcción.
_ANTIQUITY_ALIASES: tuple[str, ...] = ("antiquity", "antiguedad", "antigüedad")

#: Fallback: año explícito en la descripción ("construido en 2021", ...).
_YEAR_PATTERN = re.compile(
    r"(?:año de construcción|construid[oa]\s+en|construcción(?:\s+del)?|edificad[oa]\s+en)"
    r"[^\d]{0,15}(19\d{2}|20\d{2})",
    re.IGNORECASE,
)

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
    Idealista usa ``sale``/``homes`` con precios en string y **escalones cerrados**
    para precio y superficie, y devuelve título; Fotocasa usa ``buy``/``home`` con
    enteros, acepta cualquier valor, no publica título y anida el precio en
    ``transaction.price``. Los *builders* cubren esa variación de salida.
    """

    operation_map: Mapping[str, str]
    property_type: str
    country: str | None
    price_as_string: bool
    #: Escalones admitidos por el actor (``None`` = cualquier valor).
    price_steps: tuple[int, ...] | None = None
    size_steps: tuple[int, ...] | None = None
    #: Compone el título cuando el actor no lo devuelve.
    title_builder: Callable[[Mapping[str, Any]], str | None] | None = None
    #: Compone la ubicación cuando el actor devuelve un objeto en vez de texto.
    location_builder: Callable[[Mapping[str, Any]], str | None] | None = None
    #: Adapta la ubicación al formato que entiende el actor.
    location_transform: Callable[[str], str] | None = None


#: Escalones del esquema de ``igolaizola/idealista-scraper`` (precio en euros).
_IDEALISTA_PRICE_STEPS: tuple[int, ...] = (
    0,
    500, 550, 600, 650, 700, 750, 800, 850, 900, 950,
    1000, 1100, 1200, 1300, 1400, 1500, 1600, 1700, 1800, 1900, 2000, 2100, 2400, 2700, 3000,
    50000, 75000, 100000, 125000, 150000, 175000, 200000, 225000, 250000, 275000,
    300000, 325000, 350000, 375000, 400000, 425000, 450000, 475000, 500000,
    550000, 600000, 650000, 700000, 750000, 800000, 850000, 900000, 950000,
    1000000, 1100000, 1200000, 1300000, 1400000, 1500000, 1600000, 1700000,
    1800000, 1900000, 2000000, 2250000, 2500000, 2750000, 3000000, 3500000, 4000000,
)

#: Escalones de superficie (m²) del mismo esquema.
_IDEALISTA_SIZE_STEPS: tuple[int, ...] = (
    0, 60, 80, 100, 120, 140, 160, 180, 200, 220, 240, 260, 280, 300,
)


def _snap_to_steps(value: float, steps: Sequence[int] | None, *, round_up: bool) -> float:
    """Ajusta ``value`` a un escalón válido del actor.

    Se redondea siempre hacia el lado permisivo (mínimos hacia abajo, máximos hacia
    arriba) para no perder candidatos: ``_matches_hard_criteria`` aplica después el
    valor exacto pedido por el usuario. ``0`` significa "sin límite" en los actores.
    """
    if not steps:
        return value
    ordered = sorted(steps)
    if round_up:
        for step in ordered:
            if step >= value:
                return float(step)
        return 0.0
    allowed = [step for step in ordered if step <= value]
    return float(max(allowed)) if allowed else 0.0


def _text(value: Any) -> str | None:
    """Texto limpio de un valor escalar, o ``None`` si no aporta nada."""
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    text = str(value).strip()
    return text or None


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
        name = _text(location.get(key))
        if name and name not in parts:
            parts.append(name)
    return ", ".join(parts) or None


def _idealista_location(value: str) -> str:
    """Se queda con el primer segmento de ``"Ciudad, Provincia"``.

    El actor de Idealista resuelve ``"Madrid"`` pero no ``"Madrid, Madrid"`` (lo
    devuelve como *location not found* con status ``SUCCEEDED`` y 0 items). El
    formato con provincia es el que sugiere Fotocasa, así que se tolera aquí.
    """
    head, _, _ = value.partition(",")
    return head.strip() or value.strip()


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
        price_steps=_IDEALISTA_PRICE_STEPS,
        size_steps=_IDEALISTA_SIZE_STEPS,
        location_transform=_idealista_location,
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

_NUMBER_TOKEN = re.compile(r"-?\d[\d.,]*")


def _first_value(item: Mapping[str, Any], keys: Sequence[str]) -> Any | None:
    for key in keys:
        value = item.get(key)
        if value not in (None, "", [], {}):
            return value
    return None


def _location_text(item: Mapping[str, Any]) -> str:
    """Extrae una ubicación legible, ignorando diccionarios de coordenadas."""
    for key in ("address", "city", "municipality", "district", "place", "neighborhood"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()

    nested = item.get("location")
    if isinstance(nested, Mapping):
        # Fotocasa: calle/número + barrio/distrito/municipio en el mapa ``location``.
        parts: list[str] = []
        street = item.get("street")
        if isinstance(street, str) and street.strip():
            number = item.get("number")
            if isinstance(number, (int, str)) and str(number).strip():
                parts.append(f"{street.strip()} {str(number).strip()}".strip())
            else:
                parts.append(street.strip())
        for key in ("level8Name", "level7Name", "level5Name"):
            value = nested.get(key)
            if isinstance(value, str) and value.strip() and value.strip() not in parts:
                parts.append(value.strip())
        if parts:
            return ", ".join(parts)

    if isinstance(nested, str) and nested.strip():
        return nested.strip()

    street = item.get("street")
    if isinstance(street, str) and street.strip():
        number = item.get("number")
        if isinstance(number, (int, str)) and str(number).strip():
            return f"{street.strip()} {str(number).strip()}".strip()
        return street.strip()
    return ""


def _price_value(item: Mapping[str, Any]) -> float | None:
    """Fotocasa anida el precio en ``transaction.price``; Idealista lo deja plano."""
    transaction = item.get("transaction")
    if isinstance(transaction, Mapping):
        nested = transaction.get("price")
        if nested not in (None, ""):
            return _to_float(nested)
    return _to_float(_first_value(item, _FIELD_ALIASES["price"]))


#: Tope de fotos por anuncio: suficiente para la UI sin inflar el payload.
_MAX_PHOTOS = 6


def _photos(item: Mapping[str, Any]) -> tuple[str, ...]:
    """Extrae las URLs de fotos de la forma anidada de cada portal.

    Idealista: ``multimedia.images[].url``. Fotocasa: ``multimedia[].url``.
    Se aceptan también ``photos``/``images``/``pictures`` planos.
    """

    def collect(value: Any, found: list[str]) -> None:
        if isinstance(value, str):
            if value.startswith(("https://", "http://")):
                found.append(value)
            return
        if isinstance(value, Mapping):
            for key in ("url", "src", "image", "imageUrl"):
                nested = value.get(key)
                if isinstance(nested, str) and nested.startswith(("https://", "http://")):
                    found.append(nested)
                    return
            return
        if isinstance(value, Sequence):
            for entry in value:
                collect(entry, found)

    found: list[str] = []
    multimedia = item.get("multimedia")
    if isinstance(multimedia, Mapping):
        collect(multimedia.get("images"), found)
    else:
        collect(multimedia, found)
    if not found:
        for key in ("photos", "images", "pictures"):
            collect(item.get(key), found)

    unique: list[str] = []
    for url in found:
        if url not in unique:
            unique.append(url)
        if len(unique) >= _MAX_PHOTOS:
            break
    return tuple(unique)


def _coordinates(item: Mapping[str, Any]) -> tuple[float | None, float | None]:
    """Idealista usa lat/lon planos; Fotocasa los anida en ``location``."""
    latitude = _to_float(item.get("latitude"))
    longitude = _to_float(item.get("longitude"))
    nested = item.get("location")
    if isinstance(nested, Mapping):
        if latitude is None:
            latitude = _to_float(nested.get("latitude"))
        if longitude is None:
            longitude = _to_float(nested.get("longitude"))

    if latitude is None or longitude is None:
        return None, None
    if not (-90.0 <= latitude <= 90.0 and -180.0 <= longitude <= 180.0):
        return None, None
    return latitude, longitude


def _year_built(item: Mapping[str, Any]) -> int | None:
    """Año de construcción si el portal lo publica o la descripción lo explicita."""
    current_year = date.today().year
    candidates: list[Mapping[str, Any]] = [item]
    for key in ("_details", "features", "building", "buildingFeatures"):
        nested = item.get(key)
        if isinstance(nested, Mapping):
            candidates.append(nested)

    for mapping in candidates:
        year = _to_int(_first_value(mapping, _YEAR_ALIASES))
        if year is not None and 1800 <= year <= current_year + 1:
            return year

    # Fotocasa publica la antigüedad en años (p. ej. 7 -> construido en 2019).
    age = _to_int(_first_value(item, _ANTIQUITY_ALIASES))
    if age is not None and 0 <= age <= 200:
        return current_year - age

    description = item.get("description")
    if isinstance(description, str):
        match = _YEAR_PATTERN.search(description)
        if match is not None:
            year = int(match.group(1))
            if 1800 <= year <= current_year + 1:
                return year
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
            portal_kept = 0
            radius_dropped = 0
            radius_unknown = 0
            year_dropped = 0
            year_unknown = 0
            radius_km = criteria.radius_km
            geo_enabled = (
                radius_km is not None
                and criteria.latitude is not None
                and criteria.longitude is not None
            )
            for item in items:
                listing = self._normalize(portal, item)
                if listing is None or listing.url in seen_urls:
                    continue

                if geo_enabled:
                    distance_km = self._distance_km(listing, criteria)
                    if distance_km is None:
                        radius_unknown += 1
                    elif radius_km is not None and distance_km > radius_km:
                        radius_dropped += 1
                        continue

                if criteria.min_year_built is not None:
                    if listing.year_built is None:
                        year_unknown += 1
                    elif listing.year_built < criteria.min_year_built:
                        year_dropped += 1
                        continue

                if not self._matches_hard_criteria(listing, criteria):
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
            location=run_input.get("location"),
            duration_ms=duration_ms,
        )
        if not items:
            # El actor devuelve SUCCEEDED con 0 items cuando no resuelve la ubicación
            # (p. ej. "Madrid, Madrid" en Idealista): sin este aviso, el portal
            # desaparece en silencio de los resultados.
            self._logger.warning(
                "scraper_portal_returned_no_listings",
                portal=portal.value,
                actor_id=actor_id,
                location=run_input.get("location"),
                requested_location=criteria.location,
                error_code=None,
                duration_ms=duration_ms,
            )
        return items

    def _build_run_input(self, criteria: SearchCriteria, portal: Portal) -> dict[str, Any]:
        """Construye el input del actor según el vocabulario de cada portal.

        ``min_rooms`` no se envía: no todos los actores lo soportan y una clave
        desconocida hace fallar la validación del input. Se filtra en
        ``_matches_hard_criteria``. Precio y superficie se ajustan a los escalones
        del actor (Idealista rechaza cualquier valor intermedio con un 400).
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
            "maxItems": total_items,
        }
        if (
            criteria.latitude is not None
            and criteria.longitude is not None
            and criteria.radius_km is not None
        ):
            # Las coordenadas tienen prioridad sobre `location` en ambos actores.
            run_input["latitude"] = criteria.latitude
            run_input["longitude"] = criteria.longitude
            run_input["distanceKm"] = criteria.radius_km
        else:
            run_input["location"] = (
                profile.location_transform(criteria.location)
                if profile.location_transform is not None
                else criteria.location
            )
        if profile.country:
            run_input["country"] = profile.country

        def price(value: float) -> Any:
            return _as_int_string(value) if profile.price_as_string else int(value)

        if criteria.min_price_eur is not None:
            run_input["minPrice"] = price(
                self._snapped(
                    portal,
                    "minPrice",
                    criteria.min_price_eur,
                    profile.price_steps,
                    round_up=False,
                )
            )
        if criteria.max_price_eur is not None:
            run_input["maxPrice"] = price(
                self._snapped(
                    portal,
                    "maxPrice",
                    criteria.max_price_eur,
                    profile.price_steps,
                    round_up=True,
                )
            )
        if criteria.min_size_m2 is not None:
            min_size = self._snapped(
                portal,
                "minSize",
                criteria.min_size_m2,
                profile.size_steps,
                round_up=False,
            )
            run_input["minSize"] = _as_int_string(min_size) if profile.price_as_string else int(min_size)
        return run_input

    def _snapped(
        self,
        portal: Portal,
        field: str,
        requested: float,
        steps: Sequence[int] | None,
        *,
        round_up: bool,
    ) -> float:
        """Ajusta al escalón del actor y deja rastro solo cuando cambia el valor."""
        snapped = _snap_to_steps(requested, steps, round_up=round_up)
        if snapped != float(requested):
            self._logger.debug(
                "scraper_input_snapped",
                portal=portal.value,
                field=field,
                requested=requested,
                snapped=snapped,
            )
        return snapped

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
        price = _price_value(item)
        if not isinstance(url, str) or not url.strip() or price is None or price <= 0:
            return None

        title = _first_value(item, _FIELD_ALIASES["title"]) or _suggested_title(item)
        if not isinstance(title, str) or not title.strip():
            title = profile.title_builder(item) if profile.title_builder is not None else None

        location = _first_value(item, _FIELD_ALIASES["location"])
        if not isinstance(location, str) and profile.location_builder is not None:
            location = profile.location_builder(item)
        if not isinstance(location, str):
            location = _location_text(item)

        description = _first_value(item, _FIELD_ALIASES["description"])
        latitude, longitude = _coordinates(item)
        photos = _photos(item)
        amenities = _extract_amenities(item) + _url_amenities(url.strip())
        if photos:
            self._logger.debug(
                "listing_photos_extracted",
                portal=portal.value,
                listing_url=url.strip(),
                photos_count=len(photos),
            )

        return Listing(
            portal=portal,
            url=url.strip(),
            title=title.strip() if isinstance(title, str) and title.strip() else "Vivienda sin título",
            price_eur=price,
            location=str(location).strip() if location else "",
            size_m2=_to_float(_first_value(item, _FIELD_ALIASES["size"])),
            rooms=_to_int(_first_value(item, _FIELD_ALIASES["rooms"])),
            bathrooms=_to_int(_first_value(item, _FIELD_ALIASES["bathrooms"])),
            description=str(description).strip() if description else None,
            amenities=tuple(dict.fromkeys(amenities)),
            year_built=_year_built(item),
            latitude=latitude,
            longitude=longitude,
            photos=photos,
            raw=dict(item),
        )

    @staticmethod
    def _distance_km(listing: Listing, criteria: SearchCriteria) -> float | None:
        """Distancia al origen geográfico, o ``None`` si falta alguna coordenada."""
        if criteria.latitude is None or criteria.longitude is None:
            return None
        if listing.latitude is None or listing.longitude is None:
            return None
        origin = Coordinates(criteria.latitude, criteria.longitude)
        target = Coordinates(listing.latitude, listing.longitude)
        return origin.distance_km_to(target)

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
