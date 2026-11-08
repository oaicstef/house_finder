"""Esquemas Pydantic v2 de entrada y salida de la API.

El dominio permanece libre de frameworks: aquí se valida el transporte y se traduce a
entidades (``to_domain``) o desde ellas (``from_domain``).
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.domain.models import Portal, ScoredListing, SearchCriteria, SearchResult

__all__ = [
    "ErrorDetail",
    "ErrorResponse",
    "HealthResponse",
    "ListingResponse",
    "PortalName",
    "PortalOutcomeResponse",
    "ScheduledRunResponse",
    "SearchCreateRequest",
    "SearchProfileResponse",
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
    must_have: list[str] = Field(
        default_factory=list,
        max_length=8,
        description=(
            "Requisitos OBLIGATORIOS (semántica AND). Se buscan en título, descripción y "
            "comodidades; si un anuncio no los menciona, se descarta antes del LLM. "
            "Ejemplos: ['padel', 'piscina', 'chalet']."
        ),
        examples=[["padel", "piscina"]],
    )
    preferred_terms: list[str] = Field(
        default_factory=list,
        max_length=8,
        description=(
            "Términos deseables pero NO excluyentes: los anuncios que los mencionan se "
            "destacan en 'matched_terms' y se ordenan primero, sin descartar al resto."
        ),
        examples=[["padel"]],
    )
    postal_code: str | None = Field(
        default=None,
        pattern=r"^\d{5}$",
        description=(
            "Código postal español de 5 dígitos. Se resuelve a coordenadas y exige "
            "radius_km. No se puede combinar con latitude/longitude."
        ),
        examples=["28031"],
    )
    radius_km: int | None = Field(
        default=None,
        ge=1,
        le=50,
        description="Radio en kilómetros alrededor del código postal o de las coordenadas.",
    )
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    min_year_built: int | None = Field(
        default=None,
        ge=1800,
        le=2100,
        description=(
            "Año mínimo de construcción. Se aplica como filtro duro cuando el portal "
            "publica el año y el clasificador lo exige cuando no consta."
        ),
        examples=[2020],
    )
    max_energy_rating: Literal["A", "B", "C", "D", "E", "F", "G"] | None = Field(
        default=None,
        description=(
            "Calificación energética mínima, de A (mejor) a G (peor). 'B' acepta A o B. "
            "Se aplica como filtro duro cuando el portal publica la etiqueta y el "
            "clasificador la exige cuando no consta."
        ),
        examples=["B"],
    )

    @field_validator("must_have", "preferred_terms")
    @classmethod
    def _clean_terms(cls, values: list[str]) -> list[str]:
        cleaned: list[str] = []
        for value in values:
            term = (value or "").strip()
            if not term:
                continue
            if len(term) < 2:
                raise ValueError("cada requisito debe tener al menos 2 caracteres")
            if len(term) > 40:
                raise ValueError("cada requisito no puede superar 40 caracteres")
            if term.lower() not in {item.lower() for item in cleaned}:
                cleaned.append(term)
        return cleaned

    @model_validator(mode="after")
    def _check_price_range(self) -> Self:
        if (
            self.min_price_eur is not None
            and self.max_price_eur is not None
            and self.min_price_eur > self.max_price_eur
        ):
            raise ValueError("min_price_eur no puede ser mayor que max_price_eur")
        return self

    @field_validator("max_energy_rating", mode="before")
    @classmethod
    def _normalize_energy_rating(cls, value: Any) -> Any:
        if isinstance(value, str):
            cleaned = value.strip().upper()
            return cleaned or None
        return value

    @field_validator("min_year_built")
    @classmethod
    def _check_year_built(cls, value: int | None) -> int | None:
        if value is not None and value > date.today().year + 1:
            raise ValueError("min_year_built no puede ser posterior al año actual")
        return value

    @model_validator(mode="after")
    def _check_geo(self) -> Self:
        has_latitude = self.latitude is not None
        has_longitude = self.longitude is not None
        if has_latitude != has_longitude:
            raise ValueError("latitude y longitude deben enviarse juntas")
        if self.postal_code is not None and (has_latitude or has_longitude):
            raise ValueError("usa postal_code o latitude/longitude, no ambos")
        has_origin = self.postal_code is not None or (has_latitude and has_longitude)
        if self.radius_km is not None and not has_origin:
            raise ValueError("radius_km requiere postal_code o latitude/longitude")
        if has_origin and self.radius_km is None:
            raise ValueError("radius_km es obligatorio cuando se indica un origen geográfico")
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
            must_have=tuple(self.must_have),
            preferred_terms=tuple(self.preferred_terms),
            postal_code=self.postal_code,
            radius_km=self.radius_km,
            latitude=self.latitude,
            longitude=self.longitude,
            min_year_built=self.min_year_built,
            max_energy_rating=self.max_energy_rating,
        )

    @classmethod
    def from_domain(cls, criteria: SearchCriteria) -> "SearchCreateRequest":
        """Inverso de ``to_domain`` para devolver el perfil activo."""
        return cls(
            location=criteria.location,
            portals=[portal.value for portal in criteria.portals],
            min_price_eur=criteria.min_price_eur,
            max_price_eur=criteria.max_price_eur,
            min_size_m2=criteria.min_size_m2,
            min_rooms=criteria.min_rooms,
            max_results=criteria.max_results,
            preferences=criteria.preferences,
            must_have=list(criteria.must_have),
            preferred_terms=list(criteria.preferred_terms),
            postal_code=criteria.postal_code,
            radius_km=criteria.radius_km,
            latitude=criteria.latitude,
            longitude=criteria.longitude,
            min_year_built=criteria.min_year_built,
            max_energy_rating=criteria.max_energy_rating,
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
    year_built: int | None
    energy_rating: str | None
    photos: list[str] = Field(default_factory=list)
    amenities: list[str] = Field(default_factory=list)
    matched_terms: list[str] = Field(
        default_factory=list,
        description="Términos preferentes presentes en el anuncio (criterio no excluyente).",
    )
    relevance: float
    reason: str

    @classmethod
    def from_domain(
        cls,
        scored: ScoredListing,
        preferred_terms: tuple[str, ...] = (),
    ) -> "ListingResponse":
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
            year_built=listing.year_built,
            energy_rating=listing.energy_rating,
            photos=list(listing.photos),
            amenities=list(listing.amenities),
            matched_terms=list(listing.matches_any(preferred_terms)),
            relevance=scored.relevance,
            reason=scored.reason,
        )


class PortalOutcomeResponse(BaseModel):
    """Aportación de un portal: cuántos anuncios suyos acabaron en la respuesta."""

    portal: str
    matched: int


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
    portals: list[PortalOutcomeResponse] = Field(
        default_factory=list,
        description=(
            "Anuncios aceptados por portal solicitado. Un portal con matched=0 "
            "no aportó resultados (revisa warnings)."
        ),
    )
    warnings: list[str] = Field(
        default_factory=list,
        description="Avisos legibles derivados del resultado (p. ej. portal sin anuncios).",
    )
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
            portals=[
                PortalOutcomeResponse(portal=outcome.portal.value, matched=outcome.matched)
                for outcome in result.portal_outcomes
            ],
            warnings=list(result.warnings),
            listings=[
                ListingResponse.from_domain(item, result.criteria.preferred_terms)
                for item in result.listings
            ],
        )


class ScheduledRunResponse(BaseModel):
    """Resultado del disparo manual del job diario."""

    executed: bool
    reason: str | None = Field(
        default=None,
        description="Motivo por el que no se ejecutó (p. ej. already_executed_today)",
    )
    result: SearchResponse | None = None


class SearchProfileResponse(BaseModel):
    """Perfil de búsqueda activo (el que usa el job diario y la UI)."""

    source: Literal["override", "profile", "legacy"] = Field(
        description=(
            "override = guardado desde la UI; profile = SEARCH_PROFILE_JSON; "
            "legacy = criterios DAILY_*."
        )
    )
    profile: dict[str, Any] = Field(
        default_factory=dict,
        description="Cuerpo equivalente a POST /api/v1/searches para el perfil activo.",
    )

    @classmethod
    def from_criteria(
        cls,
        criteria: SearchCriteria,
        source: str,
    ) -> "SearchProfileResponse":
        return cls(
            source=source,
            profile=SearchCreateRequest.from_domain(criteria).model_dump(mode="json"),
        )


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
