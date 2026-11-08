"""Persistencia del perfil de búsqueda editable desde la UI.

El usuario puede cambiar los criterios preconfigurados sin tocar el ConfigMap: el
perfil guardado aquí tiene prioridad sobre ``SEARCH_PROFILE_JSON``. Se escribe junto
a los resultados (``/data/search-profile.json`` en Kubernetes), en el mismo volumen
persistente, para que sobreviva a los reinicios del pod.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Mapping

from app.domain.exceptions import RepositoryException
from app.domain.models import Portal, SearchCriteria
from app.infrastructure.logging_config import get_logger

__all__ = ["InMemorySearchProfileStore", "JsonSearchProfileStore"]


def _criteria_to_payload(criteria: SearchCriteria) -> dict[str, Any]:
    """Estructura estable y legible, idéntica al cuerpo de ``POST /searches``."""
    return {
        "location": criteria.location,
        "portals": [portal.value for portal in criteria.portals],
        "min_price_eur": criteria.min_price_eur,
        "max_price_eur": criteria.max_price_eur,
        "min_size_m2": criteria.min_size_m2,
        "min_rooms": criteria.min_rooms,
        "max_results": criteria.max_results,
        "preferences": criteria.preferences,
        "must_have": list(criteria.must_have),
        "preferred_terms": list(criteria.preferred_terms),
        "postal_code": criteria.postal_code,
        "radius_km": criteria.radius_km,
        "latitude": criteria.latitude,
        "longitude": criteria.longitude,
        "min_year_built": criteria.min_year_built,
        "max_energy_rating": criteria.max_energy_rating,
    }


def _payload_to_criteria(payload: Mapping[str, Any]) -> SearchCriteria:
    """Reconstruye los criterios; lanza ``ValueError`` si el fichero no encaja."""
    location = payload.get("location")
    if not isinstance(location, str) or not location.strip():
        raise ValueError("location es obligatoria")

    raw_portals = payload.get("portals")
    if raw_portals is None:
        portals: tuple[Portal, ...] = (Portal.IDEALISTA, Portal.FOTOCASA)
    else:
        if not isinstance(raw_portals, list) or not raw_portals:
            raise ValueError("portals debe ser una lista no vacía")
        portals = tuple(Portal(str(name)) for name in raw_portals)

    raw_must_have = payload.get("must_have") or []
    if not isinstance(raw_must_have, list):
        raise ValueError("must_have debe ser una lista")

    return SearchCriteria(
        location=location.strip(),
        portals=portals,
        min_price_eur=payload.get("min_price_eur"),
        max_price_eur=payload.get("max_price_eur"),
        min_size_m2=payload.get("min_size_m2"),
        min_rooms=payload.get("min_rooms"),
        max_results=int(payload.get("max_results") or 40),
        preferences=payload.get("preferences"),
        must_have=tuple(str(term) for term in raw_must_have),
        preferred_terms=tuple(str(term) for term in (payload.get("preferred_terms") or [])),
        postal_code=payload.get("postal_code"),
        radius_km=payload.get("radius_km"),
        latitude=payload.get("latitude"),
        longitude=payload.get("longitude"),
        min_year_built=payload.get("min_year_built"),
        max_energy_rating=payload.get("max_energy_rating"),
    )


class JsonSearchProfileStore:
    """Perfil guardado en un único fichero JSON, con escritura atómica."""

    def __init__(self, path: str | Path, *, logger: Any | None = None) -> None:
        self._path = Path(path)
        self._logger = logger or get_logger("app.search_profile")
        self._lock = asyncio.Lock()

    @property
    def path(self) -> Path:
        return self._path

    async def get(self) -> SearchCriteria | None:
        raw = await asyncio.to_thread(self._read_raw)
        if raw is None:
            return None
        try:
            return _payload_to_criteria(json.loads(raw))
        except (ValueError, TypeError, KeyError) as exc:
            # Un perfil corrupto no debe impedir arrancar: se usa el de configuración.
            self._logger.warning(
                "search_profile_store_skipped_file",
                error_code="REPOSITORY_FAILURE",
                file=self._path.name,
                error_type=type(exc).__name__,
            )
            return None

    async def save(self, criteria: SearchCriteria) -> None:
        payload = json.dumps(_criteria_to_payload(criteria), ensure_ascii=False, indent=2)
        async with self._lock:
            try:
                await asyncio.to_thread(self._write_atomic, payload)
            except OSError as exc:
                self._logger.error(
                    "search_profile_store_failed",
                    error_code="REPOSITORY_FAILURE",
                    path=str(self._path),
                    error_type=type(exc).__name__,
                )
                raise RepositoryException(
                    "No se ha podido guardar el perfil de búsqueda",
                    details={"path": str(self._path)},
                ) from exc

    async def clear(self) -> None:
        async with self._lock:
            try:
                await asyncio.to_thread(self._path.unlink, True)
            except OSError as exc:
                self._logger.error(
                    "search_profile_store_failed",
                    error_code="REPOSITORY_FAILURE",
                    path=str(self._path),
                    error_type=type(exc).__name__,
                )
                raise RepositoryException(
                    "No se ha podido restaurar el perfil de búsqueda",
                    details={"path": str(self._path)},
                ) from exc

    def _read_raw(self) -> str | None:
        try:
            return self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError as exc:
            self._logger.warning(
                "search_profile_store_unreadable",
                error_code="REPOSITORY_FAILURE",
                file=self._path.name,
                error_type=type(exc).__name__,
            )
            return None

    def _write_atomic(self, payload: str) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=str(self._path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
            os.replace(tmp_name, self._path)
        except BaseException:
            Path(tmp_name).unlink(missing_ok=True)
            raise


class InMemorySearchProfileStore:
    """Implementación de referencia (tests y despliegues sin ``RESULTS_DIR``)."""

    def __init__(self) -> None:
        self._criteria: SearchCriteria | None = None
        self._lock = asyncio.Lock()

    async def get(self) -> SearchCriteria | None:
        async with self._lock:
            return self._criteria

    async def save(self, criteria: SearchCriteria) -> None:
        async with self._lock:
            self._criteria = criteria

    async def clear(self) -> None:
        async with self._lock:
            self._criteria = None
