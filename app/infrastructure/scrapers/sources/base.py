"""Contrato de una fuente de anuncios crudos.

Una fuente conoce **un portal** y devuelve sus items tal cual los publica el medio
(JSON de su API interna, HTML con datos embebidos…). La normalización y los filtros
son responsabilidad de ``listing_scraper``, de modo que una fuente nueva no cambia
el modelo de dominio ni los criterios de búsqueda.
"""

from __future__ import annotations

from typing import Any, Mapping, Protocol, Sequence, runtime_checkable

from app.domain.models import Listing, SearchCriteria

__all__ = ["RawListingSource"]


@runtime_checkable
class RawListingSource(Protocol):
    """Proveedor de items crudos de un portal."""

    #: Nombre corto para logs y diagnóstico (``fotocasa-http``, ``apify``…).
    name: str

    async def fetch(self, criteria: SearchCriteria) -> list[Mapping[str, Any]]:
        """Items crudos que responden a ``criteria``.

        Raises:
            ScraperException: si el portal no responde, bloquea o cambia de formato.
        """
        ...

    async def enrich(self, listings: Sequence[Listing]) -> list[Listing]:
        """Añade detalles que solo están en la ficha (descripción, energía).

        Debe devolver los anuncios en el mismo orden; las fuentes sin detalle
        devuelven la secuencia tal cual.
        """
        ...
