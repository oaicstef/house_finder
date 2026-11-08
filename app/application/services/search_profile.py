"""Caso de uso: perfil de búsqueda configurable desde la UI.

Resuelve el perfil efectivo con esta precedencia:

1. perfil guardado por el usuario (``SearchProfileStore``),
2. ``SEARCH_PROFILE_JSON`` de la configuración,
3. criterios ``DAILY_*`` (compatibilidad).

El perfil efectivo lo usan la UI, ``POST /api/v1/search-profile/run`` y el job diario,
de modo que lo que el usuario guarda es exactamente lo que se ejecuta cada mañana.
"""

from __future__ import annotations

from typing import Any, Callable, Literal

from app.domain.models import SearchCriteria
from app.domain.ports import SearchProfileStore
from app.domain.validation import validate_criteria
from app.infrastructure.logging_config import get_logger

__all__ = ["ProfileSnapshot", "SearchProfileService"]

ProfileSource = Literal["override", "profile", "legacy"]


class ProfileSnapshot:
    """Criterios efectivos junto con su origen (para la respuesta de la API)."""

    __slots__ = ("criteria", "source")

    def __init__(self, criteria: SearchCriteria, source: ProfileSource) -> None:
        self.criteria = criteria
        self.source = source

    def __repr__(self) -> str:  # pragma: no cover - ayuda de depuración
        return f"ProfileSnapshot(source={self.source!r}, criteria={self.criteria!r})"


class SearchProfileService:
    """Lee, guarda y restaura el perfil de búsqueda compartido."""

    def __init__(
        self,
        store: SearchProfileStore,
        default_provider: Callable[[], tuple[SearchCriteria, ProfileSource]],
        *,
        logger: Any | None = None,
    ) -> None:
        self._store = store
        self._default_provider = default_provider
        self._logger = logger or get_logger("app.search_profile")

    async def current(self) -> ProfileSnapshot:
        """Perfil efectivo y su origen."""
        saved = await self._store.get()
        if saved is not None:
            return ProfileSnapshot(saved, "override")
        criteria, source = self._default_provider()
        return ProfileSnapshot(criteria, source)

    async def resolve_criteria(self) -> SearchCriteria:
        """Criterios efectivos (lo que ejecutan la UI y el cron)."""
        return (await self.current()).criteria

    async def update(self, criteria: SearchCriteria) -> ProfileSnapshot:
        """Valida y guarda el perfil; pasa a ser el perfil efectivo."""
        validate_criteria(criteria)
        await self._store.save(criteria)
        self._logger.info(
            "search_profile_updated",
            source="override",
            criteria_fingerprint=criteria.fingerprint(),
            portals=[portal.value for portal in criteria.portals],
            postal_code=criteria.postal_code,
        )
        return ProfileSnapshot(criteria, "override")

    async def reset(self) -> ProfileSnapshot:
        """Borra el perfil guardado y devuelve el de configuración."""
        await self._store.clear()
        criteria, source = self._default_provider()
        self._logger.info(
            "search_profile_reset",
            source=source,
            criteria_fingerprint=criteria.fingerprint(),
        )
        return ProfileSnapshot(criteria, source)
