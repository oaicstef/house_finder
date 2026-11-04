"""Esquemas Pydantic v2 de entrada y salida de la API.

El dominio permanece libre de frameworks: aquí se valida el transporte y se traduce a
entidades (``to_domain``) o desde ellas (``from_domain``).
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.domain.models import Portal, ScoredListing, SearchCriteria, SearchResult

__all__ = [
    "ErrorDetail",
    "ErrorResponse",
    "HealthResponse",
    "ListingResponse",
    "PortalName",
    "ScheduledRunResponse",
    "SearchCreateRequest",
    "SearchResponse",
]

PortalName = Literal["idealista", "fotocasa"]


class SearchCreateRequest(BaseModel):
    """Criterios enviados por el usuario para lanzar una búsqueda."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    location: str = Field(min_length=2, max_length=120, examples=["Madrid"])
    portals: list[PortalName] = Field(
        default_factory=lambda: ["idealista", "fotocasa"],
        min_length=1,
        max_length=2,
    )
    min_price_eur: float | None = Field(default=None, ge=0)
    max_price_eur: float | None = Field(default=None, ge=0)
    min_size_m2: float | None = Field(default=None, gt=0)
    min_rooms: int | None = Field(default=None, ge=0, le=20)
    max_results: int = Field(default=40, ge=1, le=100)
    preferences: str | None = Field(
        default=None,
        max_length=1000,
        examples=["Piso luminoso, con ascensor, cerca del metro"],
    )

    @model_validator(mode="after")
    def _check_price_range(self) -> Self:
        if (
            self.min_price_eur is not None
            and self.max_price_eur is not None
            and self.min_price_eur > self.max_price_eur
        ):
            raise ValueError("min_price_eur no puede ser mayor que max_price_eur")
        return self

    def to_domain(self) -> SearchCriteria:
        """Convierte el DTO de transporte en criterios de dominio."""
        unique_portals: list[Portal] = []
        for name in self.portals:
            portal = Portal(name)
            if portal not in unique_portals:
                unique_portals.append(portal)

        return SearchCriteria(
            location=self.location,
            portals=tuple(unique_portals),
            min_price_eur=self.min_price_eur,
            max_price_eur=self.max_price_eur,
            min_size_m2=self.min_size_m2,
            min_rooms=self.min_rooms,
            max_results=self.max_results,
            preferences=self.preferences,
        )


class ListingResponse(BaseModel):
    """Anuncio relevante devuelto al cliente."""

    portal: str
    url: str
    title: str
    price_eur: float
    price_per_m2: float | None
    size_m2: float | None
    rooms: int | None
    bathrooms: int | None
    location: str
    amenities: list[str] = Field(default_factory=list)
    relevance: float
    reason: str

    @classmethod
    def from_domain(cls, scored: ScoredListing) -> "ListingResponse":
        listing = scored.listing
        return cls(
            portal=listing.portal.value,
            url=listing.url,
            title=listing.title,
            price_eur=listing.price_eur,
            price_per_m2=listing.price_per_m2,
            size_m2=listing.size_m2,
            rooms=listing.rooms,
            bathrooms=listing.bathrooms,
            location=listing.location,
            amenities=list(listing.amenities),
            relevance=scored.relevance,
            reason=scored.reason,
        )


class SearchResponse(BaseModel):
    """Resultado completo de una búsqueda."""

    search_id: UUID
    criteria_fingerprint: str
    run_date: date
    created_at: datetime
    trigger: str
    status: Literal["completed"] = "completed"
    scraped_count: int
    matched_count: int
    discarded_count: int
    listings: list[ListingResponse]

    @classmethod
    def from_domain(cls, result: SearchResult) -> "SearchResponse":
        return cls(
            search_id=result.search_id,
            criteria_fingerprint=result.criteria_fingerprint,
            run_date=result.run_date,
            created_at=result.created_at,
            trigger=result.trigger.value,
            scraped_count=result.scraped_count,
            matched_count=result.matched_count,
            discarded_count=result.discarded_count,
            listings=[ListingResponse.from_domain(item) for item in result.listings],
        )


class ScheduledRunResponse(BaseModel):
    """Resultado del disparo manual del job diario."""

    executed: bool
    reason: str | None = Field(
        default=None,
        description="Motivo por el que no se ejecutó (p. ej. already_executed_today)",
    )
    result: SearchResponse | None = None


class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"
    scheduler_enabled: bool


class ErrorDetail(BaseModel):
    code: str
    message: str
    request_id: str | None = None


class ErrorResponse(BaseModel):
    """Formato uniforme de error para todos los handlers."""

    error: ErrorDetail
