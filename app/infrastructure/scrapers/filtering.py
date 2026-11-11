"""Filtros duros aplicados antes de llamar al clasificador LLM.

Se aplican sobre el anuncio ya normalizado: son baratos y evitan gastar tokens en
anuncios que no cumplen los criterios del usuario.
"""

from __future__ import annotations

from app.domain.models import Coordinates, Listing, SearchCriteria


def distance_km(listing: Listing, criteria: SearchCriteria) -> float | None:
    """Distancia al origen geográfico, o ``None`` si falta alguna coordenada."""
    if criteria.latitude is None or criteria.longitude is None:
        return None
    if listing.latitude is None or listing.longitude is None:
        return None
    origin = Coordinates(criteria.latitude, criteria.longitude)
    target = Coordinates(listing.latitude, listing.longitude)
    return origin.distance_km_to(target)




def matches_hard_criteria(listing: Listing, criteria: SearchCriteria) -> bool:
    """Filtro barato en origen; el clasificador LLM hace el filtro fino."""
    if criteria.min_price_eur is not None and listing.price_eur < criteria.min_price_eur:
        return False
    if criteria.max_price_eur is not None and listing.price_eur > criteria.max_price_eur:
        return False
    if criteria.min_size_m2 is not None:
        if listing.size_m2 is None or listing.size_m2 < criteria.min_size_m2:
            return False
    if criteria.min_rooms is not None:
        if listing.rooms is None or listing.rooms < criteria.min_rooms:
            return False
    return True
