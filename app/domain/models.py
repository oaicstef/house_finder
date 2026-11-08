"""Entidades de dominio del agente de búsqueda inmobiliaria.

Solo se usa la librería estándar: el dominio no depende de FastAPI, Pydantic ni de
ningún SDK. Los esquemas de transporte viven en ``app/api/schemas.py``.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from enum import Enum
from typing import Any, Mapping, Sequence
from uuid import UUID

__all__ = [
    "AMENITY_LABELS",
    "ENERGY_RATING_SCALE",
    "Coordinates",
    "Listing",
    "ListingAssessment",
    "Portal",
    "PortalOutcome",
    "ScoredListing",
    "SearchCriteria",
    "SearchResult",
    "Trigger",
    "normalize_text",
]


class Portal(str, Enum):
    """Portales inmobiliarios soportados."""

    IDEALISTA = "idealista"
    FOTOCASA = "fotocasa"

    @classmethod
    def parse(cls, value: str) -> "Portal":
        """Convierte un texto libre en un portal conocido."""
        normalized = str(value).strip().lower()
        for portal in cls:
            if portal.value == normalized:
                return portal
        raise ValueError(f"Portal no soportado: {value!r}")


class Trigger(str, Enum):
    """Origen de una ejecución de búsqueda."""

    API = "api"
    SCHEDULER = "scheduler"
    MANUAL_JOB = "manual-job"
    #: Ejecución del perfil configurado (botón de la UI / bajo demanda).
    PROFILE = "profile"


#: Nombre canónico de comodidad -> términos con los que el portal/usuario la nombra.
AMENITY_LABELS: dict[str, tuple[str, ...]] = {
    "swimming_pool": ("piscina", "piscinas"),
    "garden": ("jardin", "jardines", "zona ajardinada"),
    "terrace": ("terraza", "terrazas"),
    "air_conditioning": ("aire acondicionado", "climatizacion"),
    "lift": ("ascensor", "elevador"),
    "storage_room": ("trastero", "trasteros"),
}

#: Escala de certificado energético, de mejor (A) a peor (G).
ENERGY_RATING_SCALE: tuple[str, ...] = ("A", "B", "C", "D", "E", "F", "G")

_ACCENTS = str.maketrans("áéíóúüñÁÉÍÓÚÜÑ", "aeiouunAEIOUUN")


def normalize_text(text: str) -> str:
    """Minúsculas y sin acentos, para comparar términos de forma tolerante."""
    return text.translate(_ACCENTS).lower()


#: Radio medio terrestre en kilómetros (Haversine).
_EARTH_RADIUS_KM = 6371.0088


@dataclass(frozen=True, slots=True)
class Coordinates:
    """Punto geográfico WGS84 (latitud/longitud en grados decimales)."""

    latitude: float
    longitude: float

    def distance_km_to(self, other: "Coordinates") -> float:
        """Distancia en kilómetros entre dos puntos (fórmula de Haversine)."""
        lat1 = math.radians(self.latitude)
        lon1 = math.radians(self.longitude)
        lat2 = math.radians(other.latitude)
        lon2 = math.radians(other.longitude)
        delta_lat = lat2 - lat1
        delta_lon = lon2 - lon1
        a = (
            math.sin(delta_lat / 2) ** 2
            + math.cos(lat1) * math.cos(lat2) * math.sin(delta_lon / 2) ** 2
        )
        # Clamp defensivo: errores de redondeo no deben producir NaN en asin().
        return 2 * _EARTH_RADIUS_KM * math.asin(math.sqrt(min(1.0, a)))


@dataclass(frozen=True, slots=True)
class Listing:
    """Anuncio normalizado, independiente del portal de origen."""

    portal: Portal
    url: str
    title: str
    price_eur: float
    location: str
    size_m2: float | None = None
    rooms: int | None = None
    bathrooms: int | None = None
    description: str | None = None
    #: Comodidades detectadas en el anuncio (``swimming_pool``, ``garden``, ...).
    amenities: tuple[str, ...] = ()
    #: Año de construcción publicado por el portal (``None`` si no consta).
    year_built: int | None = None
    #: Certificado energético (``"A"``-``"G"``), ``None`` si el portal no lo publica.
    energy_rating: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    #: URLs ``https`` de fotos del portal (máximo 6, deduplicadas).
    photos: tuple[str, ...] = ()
    # ``compare=False`` evita que datos arbitrarios intervengan en igualdad/hash.
    raw: Mapping[str, Any] = field(default_factory=dict, compare=False, repr=False)

    @property
    def price_per_m2(self) -> float | None:
        """Precio por metro cuadrado, ``None`` si no se conoce la superficie."""
        if not self.size_m2 or self.size_m2 <= 0:
            return None
        # Redondeo comercial (mitad hacia arriba), no el bancario de round().
        value = Decimal(str(self.price_eur)) / Decimal(str(self.size_m2))
        return float(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))

    @property
    def searchable_text(self) -> str:
        """Texto donde buscar requisitos: título, descripción y amenities etiquetadas."""
        labels = " ".join(
            label for amenity in self.amenities for label in AMENITY_LABELS.get(amenity, ())
        )
        return normalize_text(f"{self.title} {self.description or ''} {labels}")

    def satisfies_all(self, terms: Sequence[str]) -> bool:
        """¿Cumple *todos* los requisitos exigidos? (semántica AND)."""
        haystack = self.searchable_text
        return all(normalize_text(term) in haystack for term in terms)

    def matches_any(self, terms: Sequence[str]) -> tuple[str, ...]:
        """Términos preferentes presentes en el anuncio (ninguno = tupla vacía)."""
        haystack = self.searchable_text
        return tuple(term for term in terms if normalize_text(term) in haystack)


@dataclass(frozen=True, slots=True)
class SearchCriteria:
    """Criterios que definen una búsqueda personalizada."""

    location: str
    portals: tuple[Portal, ...] = (Portal.IDEALISTA, Portal.FOTOCASA)
    min_price_eur: float | None = None
    max_price_eur: float | None = None
    min_size_m2: float | None = None
    min_rooms: int | None = None
    max_results: int = 40
    preferences: str | None = None
    #: Requisitos obligatorios (p. ej. ``("padel", "piscina")``). Se comprueban
    #: contra título, descripción y amenities; si falta alguno, se descarta.
    must_have: tuple[str, ...] = ()
    #: Términos deseables pero NO excluyentes: los anuncios que los mencionan se
    #: destacan y se ordenan primero, sin descartar al resto.
    preferred_terms: tuple[str, ...] = ()
    #: Origen geográfico: código postal (se resuelve a coordenadas) o lat/lon.
    postal_code: str | None = None
    radius_km: int | None = None
    latitude: float | None = None
    longitude: float | None = None
    #: Año mínimo de construcción exigido (filtro duro si el portal lo publica).
    min_year_built: int | None = None
    #: Certificado energético mínimo (``"B"`` = B o mejor; ``None`` = indiferente).
    max_energy_rating: str | None = None

    def fingerprint(self) -> str:
        """Huella estable (sha256) para la idempotencia del job diario."""
        payload = {
            "location": self.location.strip().lower(),
            "portals": sorted(portal.value for portal in self.portals),
            "min_price_eur": self.min_price_eur,
            "max_price_eur": self.max_price_eur,
            "min_size_m2": self.min_size_m2,
            "min_rooms": self.min_rooms,
            "max_results": self.max_results,
            "preferences": (self.preferences or "").strip().lower() or None,
            "must_have": sorted(normalize_text(term) for term in self.must_have),
            "preferred_terms": sorted(normalize_text(term) for term in self.preferred_terms),
            "postal_code": (self.postal_code or "").strip() or None,
            "radius_km": self.radius_km,
            "latitude": self.latitude,
            "longitude": self.longitude,
            "min_year_built": self.min_year_built,
            "max_energy_rating": (self.max_energy_rating or "").strip().upper() or None,
        }
        encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ListingAssessment:
    """Veredicto del clasificador sobre un anuncio concreto."""

    listing_url: str
    relevance: float
    matches: bool
    reason: str


@dataclass(frozen=True, slots=True)
class ScoredListing:
    """Anuncio que ha superado el filtro, con su puntuación."""

    listing: Listing
    relevance: float
    reason: str


@dataclass(frozen=True, slots=True)
class PortalOutcome:
    """Aportación de un portal a una búsqueda (para diagnóstico)."""

    portal: Portal
    matched: int

    @property
    def is_empty(self) -> bool:
        return self.matched == 0


@dataclass(frozen=True, slots=True)
class SearchResult:
    """Resultado completo y persistible de una ejecución de búsqueda."""

    search_id: UUID
    criteria: SearchCriteria
    criteria_fingerprint: str
    run_date: date
    created_at: datetime
    trigger: Trigger
    listings: tuple[ScoredListing, ...]
    scraped_count: int
    discarded_count: int

    @property
    def matched_count(self) -> int:
        return len(self.listings)

    @property
    def portal_outcomes(self) -> tuple[PortalOutcome, ...]:
        """Anuncios aceptados por cada portal solicitado, en el orden pedido."""
        counts: dict[Portal, int] = {}
        for scored in self.listings:
            counts[scored.listing.portal] = counts.get(scored.listing.portal, 0) + 1
        return tuple(
            PortalOutcome(portal=portal, matched=counts.get(portal, 0))
            for portal in self.criteria.portals
        )

    @property
    def warnings(self) -> tuple[str, ...]:
        """Avisos legibles derivados del resultado (p. ej. un portal vacío)."""
        return tuple(
            f"{outcome.portal.value}: sin anuncios; revisa el actor del portal y la ubicación"
            for outcome in self.portal_outcomes
            if outcome.is_empty
        )
