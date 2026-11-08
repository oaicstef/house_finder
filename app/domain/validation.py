"""Validaciones de dominio compartidas por los casos de uso.

``SearchService`` y ``SearchProfileService`` aplican las mismas reglas: aquí no se
importa nada de FastAPI ni de Pydantic, solo entidades y excepciones de dominio.
"""

from __future__ import annotations

import re
from datetime import date

from app.domain.exceptions import ValidationError
from app.domain.models import SearchCriteria

__all__ = ["validate_criteria"]

_POSTAL_CODE_PATTERN = re.compile(r"^\d{5}$")
_MIN_RADIUS_KM = 1
_MAX_RADIUS_KM = 50


def _validate_geo(criteria: SearchCriteria) -> None:
    """Valida la coherencia del origen geográfico y del radio."""
    postal_code = (criteria.postal_code or "").strip()
    if criteria.postal_code is not None:
        if _POSTAL_CODE_PATTERN.fullmatch(postal_code) is None:
            raise ValidationError(
                "El código postal debe tener 5 dígitos",
                code="INVALID_POSTAL_CODE",
                details={"postal_code": criteria.postal_code},
            )
        if criteria.latitude is not None or criteria.longitude is not None:
            raise ValidationError(
                "Usa postal_code o latitude/longitude, no ambos",
                details={"field": "postal_code"},
            )

    if (criteria.latitude is None) != (criteria.longitude is None):
        raise ValidationError(
            "latitude y longitude deben enviarse juntas",
            details={"field": "latitude/longitude"},
        )
    if criteria.latitude is not None and not -90 <= criteria.latitude <= 90:
        raise ValidationError(
            "latitude debe estar entre -90 y 90",
            details={"latitude": criteria.latitude},
        )
    if criteria.longitude is not None and not -180 <= criteria.longitude <= 180:
        raise ValidationError(
            "longitude debe estar entre -180 y 180",
            details={"longitude": criteria.longitude},
        )

    has_origin = postal_code != "" or criteria.latitude is not None
    if criteria.radius_km is not None and not has_origin:
        raise ValidationError(
            "radius_km requiere postal_code o latitude/longitude",
            details={"field": "radius_km"},
        )
    if has_origin and criteria.radius_km is None:
        raise ValidationError(
            "radius_km es obligatorio cuando se indica un origen geográfico",
            details={"field": "radius_km"},
        )
    if criteria.radius_km is not None and not (
        _MIN_RADIUS_KM <= criteria.radius_km <= _MAX_RADIUS_KM
    ):
        raise ValidationError(
            f"radius_km debe estar entre {_MIN_RADIUS_KM} y {_MAX_RADIUS_KM}",
            details={"radius_km": criteria.radius_km},
        )


def validate_criteria(criteria: SearchCriteria) -> None:
    """Comprueba que los criterios son ejecutables.

    Raises:
        ValidationError: con el campo culpable en ``details``.
    """
    if not criteria.location or not criteria.location.strip():
        raise ValidationError(
            "La ubicación es obligatoria",
            details={"field": "location"},
        )
    if not criteria.portals:
        raise ValidationError(
            "Debe indicarse al menos un portal",
            details={"field": "portals"},
        )
    if (
        criteria.min_price_eur is not None
        and criteria.max_price_eur is not None
        and criteria.min_price_eur > criteria.max_price_eur
    ):
        raise ValidationError(
            "El precio mínimo no puede ser mayor que el máximo",
            details={
                "min_price_eur": criteria.min_price_eur,
                "max_price_eur": criteria.max_price_eur,
            },
        )
    _validate_geo(criteria)
    if criteria.min_year_built is not None:
        current_year = date.today().year
        if not 1800 <= criteria.min_year_built <= current_year + 1:
            raise ValidationError(
                "min_year_built está fuera del rango permitido",
                details={
                    "min_year_built": criteria.min_year_built,
                    "min": 1800,
                    "max": current_year + 1,
                },
            )
