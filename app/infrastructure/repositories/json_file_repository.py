"""Repositorio en ficheros JSON.

Alternativa duradera a ``InMemoryListingRepository``: guarda cada búsqueda como
``<results_dir>/<search_id>.json`` y reconstruye el índice al arrancar, de modo que
``GET /searches/{id}/results`` y la idempotencia del job diario sobreviven a un reinicio.

No se serializa ``Listing.raw`` (payload completo del portal): es voluminoso y puede
contener datos de contacto que no deben persistirse.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

from app.domain.exceptions import RepositoryException
from app.domain.models import (
    Listing,
    Portal,
    ScoredListing,
    SearchCriteria,
    SearchResult,
    Trigger,
)
from app.infrastructure.logging_config import get_logger

__all__ = ["JsonFileListingRepository"]


class JsonFileListingRepository:
    """Persistencia en disco, segura para un único proceso."""

    def __init__(self, results_dir: str | Path, *, logger: Any | None = None) -> None:
        self._dir = Path(results_dir)
        self._logger = logger or get_logger("app.repository")
        self._index: dict[UUID, Path] = {}
        self._daily_index: dict[tuple[str, date], UUID] = {}
        self._lock = asyncio.Lock()
        self._prepare_storage()

    def _prepare_storage(self) -> None:
        """Crea el directorio y reconstruye los índices (solo al arrancar)."""
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            self._load_index()
        except OSError as exc:
            raise RepositoryException(
                "No se ha podido inicializar el almacenamiento de resultados",
                details={"results_dir": str(self._dir)},
            ) from exc

        self._logger.info(
            "results_repository_initialized",
            results_dir=str(self._dir),
            stored_results=len(self._index),
        )

    def _load_index(self) -> None:
        for path in sorted(self._dir.glob("*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                search_id = UUID(payload["search_id"])
                run_date = date.fromisoformat(payload["run_date"])
                fingerprint = payload["criteria_fingerprint"]
            except (OSError, ValueError, KeyError, TypeError):
                # Un fichero corrupto no debe impedir arrancar el resto.
                self._logger.warning(
                    "results_repository_skipped_file",
                    error_code="REPOSITORY_FAILURE",
                    file=path.name,
                )
                continue

            self._index[search_id] = path
            self._daily_index.setdefault((fingerprint, run_date), search_id)

    async def save(self, result: SearchResult) -> None:
        try:
            async with self._lock:
                payload = _serialize_result(result)
                await asyncio.to_thread(self._write_atomic, result.search_id, payload)
                self._index[result.search_id] = self._path_for(result.search_id)
                self._daily_index.setdefault(
                    (result.criteria_fingerprint, result.run_date),
                    result.search_id,
                )
        except OSError as exc:
            raise RepositoryException(
                "No se ha podido guardar el resultado de la búsqueda",
                details={"search_id": str(result.search_id)},
            ) from exc

        self._logger.info(
            "search_result_persisted",
            search_id=str(result.search_id),
            matched_count=result.matched_count,
            storage="json_file",
        )

    async def get_by_id(self, search_id: UUID) -> SearchResult | None:
        async with self._lock:
            path = self._index.get(search_id)
        if path is None:
            return None
        return await self._read(path, search_id)

    async def find_by_criteria_and_day(
        self,
        criteria_fingerprint: str,
        run_date: date,
    ) -> SearchResult | None:
        async with self._lock:
            search_id = self._daily_index.get((criteria_fingerprint, run_date))
        if search_id is None:
            return None
        return await self.get_by_id(search_id)

    async def list_recent(self, limit: int = 20) -> list[SearchResult]:
        """Búsquedas guardadas, de la más reciente a la más antigua.

        Se ordena por ``created_at`` y no por ``mtime``: dos escrituras seguidas
        pueden compartir la misma marca de tiempo del sistema de ficheros y dejar
        el orden indefinido.
        """
        async with self._lock:
            paths = list(self._index.values())
        results: list[SearchResult] = []
        for path in paths:
            try:
                results.append(await self._read(path, UUID(path.stem)))
            except RepositoryException:
                continue
        results.sort(key=lambda result: result.created_at, reverse=True)
        return results[:limit]

    async def find_latest(self) -> SearchResult | None:
        """Búsqueda más reciente en disco; tolera ficheros corruptos intermedios."""
        recent = await self.list_recent(limit=1)
        return recent[0] if recent else None

    def _path_for(self, search_id: UUID) -> Path:
        return self._dir / f"{search_id}.json"

    def _write_atomic(self, search_id: UUID, payload: dict[str, Any]) -> None:
        """Escritura atómica: un fallo a medias no deja un JSON corrupto."""
        target = self._path_for(search_id)
        fd, tmp_name = tempfile.mkstemp(dir=str(self._dir), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
            os.replace(tmp_name, target)
        except BaseException:
            Path(tmp_name).unlink(missing_ok=True)
            raise

    async def _read(self, path: Path, search_id: UUID) -> SearchResult:
        try:
            raw = await asyncio.to_thread(path.read_text, encoding="utf-8")
            return _deserialize_result(json.loads(raw))
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise RepositoryException(
                "No se ha podido leer el resultado de la búsqueda",
                details={"search_id": str(search_id), "file": path.name},
            ) from exc


def _serialize_result(result: SearchResult) -> dict[str, Any]:
    """Estructura estable y legible; omite ``raw`` a propósito."""
    criteria = result.criteria
    return {
        "search_id": str(result.search_id),
        "criteria_fingerprint": result.criteria_fingerprint,
        "run_date": result.run_date.isoformat(),
        "created_at": result.created_at.isoformat(),
        "trigger": result.trigger.value,
        "scraped_count": result.scraped_count,
        "discarded_count": result.discarded_count,
        "matched_count": result.matched_count,
        "origin": {
            "latitude": result.origin_latitude,
            "longitude": result.origin_longitude,
        },
        "criteria": {
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
        },
        "listings": [
            {
                "portal": scored.listing.portal.value,
                "url": scored.listing.url,
                "title": scored.listing.title,
                "price_eur": scored.listing.price_eur,
                "size_m2": scored.listing.size_m2,
                "rooms": scored.listing.rooms,
                "bathrooms": scored.listing.bathrooms,
                "location": scored.listing.location,
                "year_built": scored.listing.year_built,
                "energy_rating": scored.listing.energy_rating,
                # Necesarias para calcular distance_km al releer el resultado.
                "latitude": scored.listing.latitude,
                "longitude": scored.listing.longitude,
                "photos": list(scored.listing.photos),
                "description": scored.listing.description,
                "amenities": list(scored.listing.amenities),
                "price_per_m2": scored.listing.price_per_m2,
                "relevance": scored.relevance,
                "reason": scored.reason,
            }
            for scored in result.listings
        ],
    }


def _deserialize_result(payload: dict[str, Any]) -> SearchResult:
    criteria_payload = payload["criteria"]
    criteria = SearchCriteria(
        location=criteria_payload["location"],
        portals=tuple(Portal(name) for name in criteria_payload["portals"]),
        min_price_eur=criteria_payload.get("min_price_eur"),
        max_price_eur=criteria_payload.get("max_price_eur"),
        min_size_m2=criteria_payload.get("min_size_m2"),
        min_rooms=criteria_payload.get("min_rooms"),
        max_results=criteria_payload.get("max_results", 40),
        preferences=criteria_payload.get("preferences"),
        must_have=tuple(criteria_payload.get("must_have") or ()),
        preferred_terms=tuple(criteria_payload.get("preferred_terms") or ()),
        postal_code=criteria_payload.get("postal_code"),
        radius_km=criteria_payload.get("radius_km"),
        latitude=criteria_payload.get("latitude"),
        longitude=criteria_payload.get("longitude"),
        min_year_built=criteria_payload.get("min_year_built"),
        max_energy_rating=criteria_payload.get("max_energy_rating"),
    )
    listings = tuple(
        ScoredListing(
            listing=Listing(
                portal=Portal(item["portal"]),
                url=item["url"],
                title=item["title"],
                price_eur=float(item["price_eur"]),
                location=item.get("location", ""),
                size_m2=item.get("size_m2"),
                rooms=item.get("rooms"),
                bathrooms=item.get("bathrooms"),
                description=item.get("description"),
                amenities=tuple(item.get("amenities") or ()),
                year_built=item.get("year_built"),
                energy_rating=item.get("energy_rating"),
                latitude=item.get("latitude"),
                longitude=item.get("longitude"),
                photos=tuple(item.get("photos") or ()),
            ),
            relevance=float(item["relevance"]),
            reason=item.get("reason", ""),
        )
        for item in payload["listings"]
    )
    created_at = datetime.fromisoformat(payload["created_at"])
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    origin = payload.get("origin") or {}

    return SearchResult(
        search_id=UUID(payload["search_id"]),
        criteria=criteria,
        criteria_fingerprint=payload["criteria_fingerprint"],
        run_date=date.fromisoformat(payload["run_date"]),
        created_at=created_at,
        trigger=Trigger(payload["trigger"]),
        listings=listings,
        scraped_count=int(payload.get("scraped_count", len(listings))),
        discarded_count=int(payload.get("discarded_count", 0)),
        origin_latitude=origin.get("latitude"),
        origin_longitude=origin.get("longitude"),
    )
