"""Endpoints HTTP.

Capa deliberadamente delgada: validan el transporte, delegan en el servicio
inyectado y traducen el resultado de dominio al esquema de respuesta.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Path, status

from app.api.dependencies import (
    ContainerDep,
    ScheduledSearchServiceDep,
    SearchServiceDep,
)
from app.api.schemas import (
    ErrorResponse,
    HealthResponse,
    ScheduledRunResponse,
    SearchCreateRequest,
    SearchProfileResponse,
    SearchResponse,
)
from app.domain.exceptions import ConfigurationException
from app.domain.models import Trigger

__all__ = ["health_router", "router"]

router = APIRouter(prefix="/api/v1", tags=["searches"])
health_router = APIRouter(tags=["health"])

_ERROR_RESPONSES: dict[int | str, dict[str, object]] = {
    422: {"model": ErrorResponse},
    status.HTTP_502_BAD_GATEWAY: {"model": ErrorResponse},
    status.HTTP_503_SERVICE_UNAVAILABLE: {"model": ErrorResponse},
    status.HTTP_500_INTERNAL_SERVER_ERROR: {"model": ErrorResponse},
}


@router.post(
    "/searches",
    response_model=SearchResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Ejecuta una búsqueda síncrona",
    responses=_ERROR_RESPONSES,
)
async def create_search(
    payload: SearchCreateRequest,
    service: SearchServiceDep,
) -> SearchResponse:
    """Scrapea los portales, clasifica los anuncios y devuelve los relevantes."""
    result = await service.run_search(payload.to_domain(), trigger=Trigger.API.value)
    return SearchResponse.from_domain(result)


@router.get(
    "/searches/{search_id}/results",
    response_model=SearchResponse,
    summary="Devuelve los resultados de una búsqueda previa",
    responses={status.HTTP_404_NOT_FOUND: {"model": ErrorResponse}, **_ERROR_RESPONSES},
)
async def get_search_results(
    search_id: Annotated[UUID, Path(description="Identificador de la búsqueda")],
    service: SearchServiceDep,
) -> SearchResponse:
    result = await service.get_results(search_id)
    return SearchResponse.from_domain(result)


@router.get(
    "/search-profile",
    response_model=SearchProfileResponse,
    summary="Devuelve el perfil de búsqueda activo",
    responses=_ERROR_RESPONSES,
)
async def get_search_profile(container: ContainerDep) -> SearchProfileResponse:
    """Perfil que usan el job diario y la UI (``SEARCH_PROFILE_JSON``)."""
    profile = container.settings.build_search_profile()
    if profile is None:
        raise ConfigurationException(
            "SEARCH_PROFILE_JSON no está configurado; revisa DAILY_*",
            details={"missing": ["SEARCH_PROFILE_JSON"]},
        )
    return SearchProfileResponse(source="profile", profile=profile)


@router.post(
    "/search-profile/run",
    response_model=SearchResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Ejecuta el perfil de búsqueda configurado",
    responses=_ERROR_RESPONSES,
)
async def run_search_profile(
    service: SearchServiceDep,
    container: ContainerDep,
) -> SearchResponse:
    """Lanza la búsqueda definida en ``SEARCH_PROFILE_JSON`` (o en ``DAILY_*``)."""
    criteria = container.settings.build_daily_criteria()
    result = await service.run_search(criteria, trigger=Trigger.PROFILE.value)
    return SearchResponse.from_domain(result)


@router.post(
    "/scheduled-searches/run",
    response_model=ScheduledRunResponse,
    summary="Dispara manualmente la búsqueda diaria",
    responses=_ERROR_RESPONSES,
)
async def run_scheduled_search(
    service: ScheduledSearchServiceDep,
) -> ScheduledRunResponse:
    """Ejecuta el job diario; si ya se ejecutó hoy devuelve ``executed=false``."""
    result = await service.run_daily_job(raise_on_error=True)
    if result is None:
        return ScheduledRunResponse(executed=False, reason="already_executed_today")
    return ScheduledRunResponse(
        executed=True,
        reason=None,
        result=SearchResponse.from_domain(result),
    )


@health_router.get("/health", response_model=HealthResponse, tags=["health"])
async def health(container: ContainerDep) -> HealthResponse:
    return HealthResponse(status="ok", scheduler_enabled=container.settings.enable_scheduler)
