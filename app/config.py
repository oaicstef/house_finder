"""Configuración tipada de la aplicación.

Se valida al arrancar: si falta una clave obligatoria se lanza
``ConfigurationException`` (nunca ``KeyError``) con el listado de variables.
"""

from __future__ import annotations

import json
import re
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from dotenv import load_dotenv
from pydantic import Field, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.domain.exceptions import ConfigurationException
from app.domain.models import Portal, SearchCriteria

# Carga ``.env`` antes de instanciar Settings para no depender del entorno del shell.
load_dotenv()

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
Environment = Literal["development", "production", "test"]

#: Perfil de búsqueda por defecto (job diario y UI). Se puede sustituir con
#: ``SEARCH_PROFILE_JSON`` sin tocar código; ``SEARCH_PROFILE_JSON=`` vacío vuelve a
#: los criterios ``DAILY_*``.
DEFAULT_SEARCH_PROFILE: dict[str, Any] = {
    "location": "Madrid",
    "portals": ["idealista", "fotocasa"],
    "max_price_eur": 900000,
    "min_size_m2": 140,
    "min_rooms": 4,
    "max_results": 50,
    "preferences": "Casa o chalet, padel cominitario, piscina comunitaria",
    "must_have": ["padel"],
    "postal_code": "28031",
    "radius_km": 5,
    "min_year_built": 2020,
}

DEFAULT_SEARCH_PROFILE_JSON = json.dumps(DEFAULT_SEARCH_PROFILE, ensure_ascii=False)

# Variable de entorno -> atributo del modelo.
REQUIRED_SECRETS: dict[str, str] = {
    "APIFY_TOKEN": "apify_token",
    "DEEPSEEK_API_KEY": "deepseek_api_key",
}


class Settings(BaseSettings):
    """Ajustes de la aplicación leídos de variables de entorno."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    environment: Environment = "development"
    log_level: LogLevel = "INFO"
    timezone: str = "Europe/Madrid"
    enable_scheduler: bool = True

    apify_token: str = ""
    # Valores reales verificados en Apify: un placeholder aquí provoca 404 y tumba la
    # búsqueda completa cuando la variable no está definida. Vacío = portal omitido.
    apify_idealista_actor: str = "igolaizola/idealista-scraper"
    apify_fotocasa_actor: str = ""
    search_operation: Literal["rent", "sale"] = "rent"
    apify_max_items: int = Field(default=40, ge=1, le=200)
    apify_timeout_seconds: int = Field(default=300, ge=10, le=900)
    # Tope de gasto por ejecución de actor (None = sin límite explícito).
    apify_max_charge_usd: float | None = Field(default=None, gt=0)

    # Geocodificación de códigos postales (CartoCiudad).
    geocoding_base_url: str = "https://www.cartociudad.es/geocoder/api/geocoder"
    geocoding_timeout_seconds: float = Field(default=10.0, gt=0)

    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-flash"
    deepseek_timeout_seconds: float = Field(default=60.0, gt=0)

    daily_search_location: str = "Madrid"
    daily_search_portals: str = "idealista,fotocasa"
    daily_max_price_eur: float | None = 1500.0
    daily_min_size_m2: float | None = 60.0
    daily_min_rooms: int | None = 2
    daily_max_results: int = Field(default=40, ge=1, le=100)
    daily_preferences: str | None = None
    #: Requisitos obligatorios del cron, separados por comas (vacío = sin filtro duro).
    daily_must_have: str | None = None
    #: Radio geográfico del cron (CP y radio deben configurarse juntos).
    daily_postal_code: str | None = None
    daily_radius_km: int | None = Field(default=None, ge=1, le=50)
    daily_min_year_built: int | None = Field(default=None, ge=1800, le=2100)

    relevance_threshold: float = Field(default=0.6, ge=0.0, le=1.0)
    # Directorio donde persistir los resultados (vacío = solo en memoria).
    results_dir: str = "data/results"
    #: Fichero del perfil editable desde la UI (vacío = junto a ``results_dir``).
    search_profile_file: str | None = None
    #: Perfil de búsqueda compartido (job diario + UI + ejecución bajo demanda).
    search_profile_json: str | None = DEFAULT_SEARCH_PROFILE_JSON

    @field_validator(
        "apify_max_charge_usd",
        "daily_max_price_eur",
        "daily_min_size_m2",
        "daily_min_rooms",
        "daily_preferences",
        "daily_must_have",
        "daily_postal_code",
        "daily_radius_km",
        "daily_min_year_built",
        "search_profile_json",
        "search_profile_file",
        mode="before",
    )
    @classmethod
    def _blank_to_none(cls, value: Any) -> Any:
        """``VAR=`` en el ``.env`` significa "sin definir", no un valor vacío."""
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("geocoding_base_url")
    @classmethod
    def _check_geocoding_base_url(cls, value: str) -> str:
        """El geocodificador no puede quedar sin URL."""
        stripped = value.strip()
        if not stripped:
            raise ValueError("GEOCODING_BASE_URL no puede estar vacío")
        return stripped

    @model_validator(mode="after")
    def _validate_required_secrets(self) -> "Settings":
        """Falla al arrancar si faltan credenciales (salvo en entorno de test)."""
        if self.environment == "test":
            return self
        missing = [
            env_name
            for env_name, attribute in REQUIRED_SECRETS.items()
            if not str(getattr(self, attribute) or "").strip()
        ]
        if missing:
            raise ConfigurationException(
                "Faltan variables de entorno obligatorias: " + ", ".join(missing),
                details={"missing": missing},
            )
        return self

    @property
    def json_logs(self) -> bool:
        """JSON en producción, consola legible en el resto de entornos."""
        return self.environment == "production"

    @property
    def resolved_search_profile_file(self) -> str | None:
        """Ruta del perfil editable; por defecto, junto al directorio de resultados.

        Con ``RESULTS_DIR=/data/results`` (PVC de Kubernetes) el perfil queda en
        ``/data/search-profile.json``, dentro del mismo volumen persistente.
        """
        explicit = (self.search_profile_file or "").strip()
        if explicit:
            return explicit
        results_dir = (self.results_dir or "").strip()
        if not results_dir:
            return None
        return str(Path(results_dir).parent / "search-profile.json")

    def build_search_profile(self) -> dict[str, Any] | None:
        """Perfil de búsqueda configurado (``None`` si se usa la ruta ``DAILY_*``).

        Raises:
            ConfigurationException: si el JSON no es válido o no cumple el contrato de
                ``POST /api/v1/searches``.
        """
        raw = (self.search_profile_json or "").strip()
        if not raw:
            return None
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ConfigurationException(
                "SEARCH_PROFILE_JSON no es un JSON válido",
                details={"error": exc.msg, "position": exc.pos},
            ) from exc
        if not isinstance(payload, dict):
            raise ConfigurationException(
                "SEARCH_PROFILE_JSON debe ser un objeto JSON",
                details={"type": type(payload).__name__},
            )

        # Import perezoso: la capa API depende de config, no al revés en el import.
        from app.api.schemas import SearchCreateRequest

        try:
            SearchCreateRequest.model_validate(payload)
        except ValidationError as exc:
            raise ConfigurationException(
                "SEARCH_PROFILE_JSON no cumple el contrato de búsqueda",
                details={
                    "fields": [
                        {
                            "field": ".".join(str(part) for part in error["loc"]) or "body",
                            "msg": error["msg"],
                        }
                        for error in exc.errors()
                    ]
                },
            ) from exc
        return payload

    def build_daily_criteria(self) -> SearchCriteria:
        """Criterios del job diario: perfil compartido o, si no hay, ``DAILY_*``."""
        profile = self.build_search_profile()
        if profile is not None:
            from app.api.schemas import SearchCreateRequest

            return SearchCreateRequest.model_validate(profile).to_domain()
        return self._build_legacy_daily_criteria()

    def _build_legacy_daily_criteria(self) -> SearchCriteria:
        """Criterios del cron a partir de ``DAILY_*`` (compatibilidad)."""
        portals: list[Portal] = []
        for raw_portal in self.daily_search_portals.split(","):
            if not raw_portal.strip():
                continue
            try:
                portals.append(Portal.parse(raw_portal))
            except ValueError as exc:
                raise ConfigurationException(
                    f"DAILY_SEARCH_PORTALS contiene un portal no soportado: {raw_portal!r}",
                    details={"invalid_portal": raw_portal},
                ) from exc
        if not portals:
            raise ConfigurationException(
                "DAILY_SEARCH_PORTALS debe contener al menos un portal",
                details={"missing": ["DAILY_SEARCH_PORTALS"]},
            )

        preferences = (self.daily_preferences or "").strip() or None
        postal_code = (self.daily_postal_code or "").strip() or None
        radius_km = self.daily_radius_km
        if (postal_code is None) != (radius_km is None):
            raise ConfigurationException(
                "DAILY_POSTAL_CODE y DAILY_RADIUS_KM deben configurarse juntos",
                details={
                    "daily_postal_code": postal_code,
                    "daily_radius_km": radius_km,
                },
            )
        if postal_code is not None and re.fullmatch(r"\d{5}", postal_code) is None:
            raise ConfigurationException(
                "DAILY_POSTAL_CODE debe tener 5 dígitos",
                details={"daily_postal_code": postal_code},
            )
        if self.daily_min_year_built is not None:
            current_year = date.today().year
            if not 1800 <= self.daily_min_year_built <= current_year + 1:
                raise ConfigurationException(
                    "DAILY_MIN_YEAR_BUILT está fuera del rango permitido",
                    details={
                        "daily_min_year_built": self.daily_min_year_built,
                        "min": 1800,
                        "max": current_year + 1,
                    },
                )

        return SearchCriteria(
            location=self.daily_search_location,
            portals=tuple(portals),
            max_price_eur=self.daily_max_price_eur,
            min_size_m2=self.daily_min_size_m2,
            min_rooms=self.daily_min_rooms,
            max_results=self.daily_max_results,
            preferences=preferences,
            must_have=self._parse_daily_must_have(),
            postal_code=postal_code,
            radius_km=radius_km,
            min_year_built=self.daily_min_year_built,
        )

    def _parse_daily_must_have(self) -> tuple[str, ...]:
        """Convierte ``DAILY_MUST_HAVE`` (lista separada por comas) en requisitos.

        Raises:
            ConfigurationException: si algún término no cumple el contrato de longitud
                (2-40 caracteres) que también aplica la API.
        """
        terms: list[str] = []
        seen: set[str] = set()
        for raw_term in (self.daily_must_have or "").split(","):
            term = raw_term.strip()
            if not term:
                continue
            if not 2 <= len(term) <= 40:
                raise ConfigurationException(
                    "DAILY_MUST_HAVE contiene un requisito con longitud inválida "
                    f"(2-40 caracteres): {term!r}",
                    details={"invalid_requirement": term},
                )
            key = term.lower()
            if key not in seen:
                seen.add(key)
                terms.append(term)
        return tuple(terms)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Devuelve la configuración (cacheada) traduciendo errores a dominio."""
    try:
        return Settings()
    except ValidationError as exc:
        raise ConfigurationException(
            "Configuración inválida: revisa las variables de entorno",
            details={"errors": [error["msg"] for error in exc.errors()]},
        ) from exc
