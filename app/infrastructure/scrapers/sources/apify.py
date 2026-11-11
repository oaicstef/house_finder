"""Fuente de anuncios basada en actores de Apify (proveedor de respaldo).

Cada instancia representa **un portal**: lanza el actor configurado, espera el dataset
y devuelve los items crudos tal cual llegan. La normalización y los filtros viven en
``listing_scraper``/``normalization``, así que esta clase solo se ocupa del proveedor.
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from typing import Any, Callable, Mapping

from app.domain.exceptions import ScraperException
from app.domain.models import Listing, Portal, SearchCriteria
from app.infrastructure.logging_config import get_logger
from app.infrastructure.scrapers.normalization import (
    _PROFILES,
    _as_int_string,
    _energy_rating,
    _merged_description,
    _snap_to_steps,
)

__all__ = ["ApifyPortalSource"]

#: Estados terminales de un run de Apify que implican que no hay datos utilizables.
_FAILED_RUN_STATUSES = frozenset({"FAILED", "ABORTED", "TIMED-OUT"})


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


class ApifyPortalSource:
    """Items crudos de un portal, delegando en su actor de Apify."""

    name = "apify"

    def __init__(
        self,
        *,
        portal: Portal,
        token: str,
        actor_id: str,
        max_items: int = 40,
        timeout_seconds: int = 300,
        operation: str = "rent",
        max_charge_usd: float | None = None,
        client_factory: Callable[[str], Any] = _default_client_factory,
        logger: Any | None = None,
    ) -> None:
        self._portal = portal
        self._token = token
        self._actor_id = actor_id
        self._max_items = max_items
        self._timeout_seconds = timeout_seconds
        self._operation = operation
        self._max_charge_usd = max_charge_usd
        self._client_factory = client_factory
        self._logger = logger or get_logger("app.scraper")

    @staticmethod
    def _elapsed_ms(started: float) -> float:
        return round((time.perf_counter() - started) * 1000, 2)


    async def fetch(self, criteria: SearchCriteria) -> list[dict[str, Any]]:
        """Devuelve los items crudos del actor configurado para este portal."""
        portal = self._portal
        actor_id = self._actor_id
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

    async def enrich(self, listings: list[Listing]) -> list[Listing]:
        """Añade las variantes de descripción de ``_details`` a cada anuncio."""
        portal = self._portal
        if portal is not Portal.IDEALISTA:
            # Solo el detalle de Idealista publica el certificado y las versiones
            # lingüísticas de la descripción.
            return listings
        actor_id = self._actor_id
        if not actor_id:
            return listings

        codes: dict[str, Listing] = {}
        for listing in listings:
            if listing.portal is not Portal.IDEALISTA:
                continue
            match = re.search(r"/inmueble/(\d+)", listing.url)
            if match:
                codes[match.group(1)] = listing
        if not codes:
            return listings

        started = time.perf_counter()
        run_input = {
            "propertyCodes": list(codes),
            "country": "es",
            "operation": self._operation,
            "propertyType": "homes",
        }
        try:
            items = await asyncio.to_thread(self._run_actor, actor_id, run_input)
        except ScraperException as exc:
            self._logger.warning(
                "listing_details_failed",
                error_code=exc.code,
                candidate_count=len(codes),
                duration_ms=self._elapsed_ms(started),
            )
            return listings

        enriched_by_code: dict[str, Listing] = {}
        for item in items:
            code = str(item.get("propertyCode") or "")
            if code not in codes:
                continue
            enriched_by_code[code] = replace(
                codes[code],
                description=_merged_description(item) or codes[code].description,
                energy_rating=_energy_rating(item) or codes[code].energy_rating,
            )

        enriched = [
            enriched_by_code.get(code, listing) for code, listing in codes.items()
        ]
        self._logger.info(
            "listing_details_enriched",
            candidate_count=len(codes),
            enriched_count=len(enriched_by_code),
            duration_ms=self._elapsed_ms(started),
        )
        # Mantiene el orden original de la lista.
        by_url = {listing.url: listing for listing in enriched}
        return [by_url.get(listing.url, listing) for listing in listings]

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
        if criteria.home_types and profile.supports_home_type:
            run_input["homeType"] = list(criteria.home_types)
        if criteria.max_energy_rating is not None and profile.supports_fetch_details:
            # Solo el detalle de Idealista publica el certificado; cuesta 1 request
            # extra por inmueble, así que se activa únicamente cuando hace falta.
            run_input["fetchDetails"] = True
            self._logger.debug(
                "scraper_fetch_details_enabled",
                portal=portal.value,
                reason="max_energy_rating",
            )
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
