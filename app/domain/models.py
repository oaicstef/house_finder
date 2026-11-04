"""Entidades de dominio del agente de búsqueda inmobiliaria.

Solo se usa la librería estándar: el dominio no depende de FastAPI, Pydantic ni de
ningún SDK. Los esquemas de transporte viven en ``app/api/schemas.py``.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from enum import Enum
from typing import Any, Mapping
from uuid import UUID

__all__ = [
    "Listing",
    "ListingAssessment",
    "Portal",
    "ScoredListing",
    "SearchCriteria",
    "SearchResult",
    "Trigger",
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
