"""Normalización de anuncios crudos al modelo de dominio.

Los proveedores de scraping (los actores de Apify o el HTTP propio de cada portal)
producen *items crudos* con el vocabulario de su portal; este módulo los traduce a
``Listing`` sin saber de dónde vienen. Se extrajo del adaptador de Apify para poder
cambiar de proveedor sin tocar la normalización ni el dominio.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Any, Callable, Literal, Mapping, Sequence

from app.domain.models import AMENITY_LABELS, Listing, Portal, normalize_text, parse_home_type
from app.infrastructure.logging_config import get_logger

_logger = get_logger("app.scraper")

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

#: Claves donde Idealista/Fotocasa pueden publicar el certificado energético.
_ENERGY_KEYS: tuple[str, ...] = (
    "energyCertification",
    "energyCertificate",
    "certificadoEnergetico",
    "certificado_energetico",
    "energyRating",
    "energyClass",
    "energyLabel",
    "energyEfficiency",
    "energyCertificationType",
    "energyPerformance",
)

#: "B", "b", "Clase B", "B - 45 kWh/m²"... pero nunca la C de "certificate".
_ENERGY_RATING_PATTERN = re.compile(r"^\s*(?:clase\s*)?([A-G])\b", re.IGNORECASE)

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
    #: El actor admite ``fetchDetails`` (necesario para leer el certificado energético).
    supports_fetch_details: bool = False
    #: El actor admite ``homeType`` (filtro por subtipo de vivienda).
    supports_home_type: bool = False


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
        supports_fetch_details=True,
        supports_home_type=True,
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


def _energy_rating_from_value(value: Any) -> str | None:
    """Letra A-G de una etiqueta energética, o ``None`` si el valor no la contiene."""
    if isinstance(value, str):
        match = _ENERGY_RATING_PATTERN.match(value)
        return match.group(1).upper() if match else None
    if isinstance(value, Mapping):
        # Idealista publica la letra en ``energyConsumption.type`` ("e"); otros
        # portales usan ``rating``/``value``/``label``.
        for key in (
            "energyConsumption",
            "rating",
            "energyLevel",
            "type",
            "level",
            "value",
            "label",
        ):
            rating = _energy_rating_from_value(value.get(key))
            if rating:
                return rating
    return None


def _home_type(item: Mapping[str, Any]) -> str | None:
    """Tipo de vivienda canónico a partir del subtipo del portal."""
    detailed = item.get("detailedType")
    if isinstance(detailed, Mapping):
        for key in ("subTypology", "typology"):
            value = detailed.get(key)
            parsed = parse_home_type(value) if isinstance(value, str) else None
            if parsed:
                return parsed
    for key in ("propertySubtype", "propertyType", "homeType"):
        value = item.get(key)
        parsed = parse_home_type(value) if isinstance(value, str) else None
        if parsed:
            return parsed
    return None


def _listing_condition(item: Mapping[str, Any]) -> Literal["new", "second_hand"] | None:
    """Interpreta señales explícitas de obra nueva o segunda mano."""
    for key in (
        "newDevelopment",
        "isNewDevelopment",
        "newConstruction",
        "isNewConstruction",
        "isNewBuild",
        "isNew",
        "new_development",
        "is_new",
    ):
        if key not in item:
            continue
        value = item[key]
        if isinstance(value, bool):
            return "new" if value else "second_hand"
        condition = _condition_from_text(value)
        if condition is not None:
            return condition

    for key in ("listingCondition", "propertyCondition", "condition"):
        condition = _condition_from_text(item.get(key))
        if condition is not None:
            return condition
    return None


def _condition_from_text(value: Any) -> Literal["new", "second_hand"] | None:
    """Devuelve una condición solo cuando el texto es una etiqueta reconocida."""
    if not isinstance(value, str):
        return None
    normalized = normalize_text(value).strip().lower().replace("-", " ")
    if normalized in {
        "new",
        "obra nueva",
        "nueva construccion",
        "a estrenar",
        "new build",
        "new construction",
        "new development",
    }:
        return "new"
    if normalized in {
        "second hand",
        "segunda mano",
        "used",
        "usado",
        "usada",
        "resale",
    }:
        return "second_hand"
    return None


def _merged_description(item: Mapping[str, Any]) -> str | None:
    """Fusiona la descripción del listado con ``_details`` y sus versiones."""
    texts: list[str] = []
    primary = _first_value(item, _FIELD_ALIASES["description"])
    if isinstance(primary, str) and primary.strip():
        texts.append(primary.strip())
    details = item.get("_details")
    if isinstance(details, Mapping):
        value = details.get("propertyComment")
        if isinstance(value, str) and value.strip():
            texts.append(value.strip())
        comments = details.get("comments")
        if isinstance(comments, list):
            for comment in comments:
                if isinstance(comment, Mapping):
                    value = comment.get("propertyComment")
                    if isinstance(value, str) and value.strip():
                        texts.append(value.strip())
    return "\n".join(dict.fromkeys(texts)) or None


def _energy_rating(item: Mapping[str, Any]) -> str | None:
    """Busca el certificado en el item y, si existe, en ``_details`` (Idealista)."""
    sources: list[Mapping[str, Any]] = [item]
    details = item.get("_details")
    if isinstance(details, Mapping):
        sources.append(details)
        extra = details.get("moreCharacteristics")
        if isinstance(extra, Mapping):
            sources.append(extra)
    for source in sources:
        for key in _ENERGY_KEYS:
            rating = _energy_rating_from_value(source.get(key))
            if rating:
                return rating
    return None


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


def normalize_listing(portal: Portal, item: Mapping[str, Any]) -> Listing | None:
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

    description = _merged_description(item)
    latitude, longitude = _coordinates(item)
    photos = _photos(item)
    amenities = _extract_amenities(item) + _url_amenities(url.strip())
    if photos:
        _logger.debug(
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
        energy_rating=_energy_rating(item),
        listing_condition=_listing_condition(item),
        home_type=_home_type(item),
        latitude=latitude,
        longitude=longitude,
        photos=photos,
        raw=dict(item),
    )
