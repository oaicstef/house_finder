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
    "preferred_terms": [],
    "home_types": [],
    "postal_code": "28031",
    "radius_km": 5,
    "min_year_built": 2020,
    "max_energy_rating": None,
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
    #: Credenciales para la Search API oficial (solo tras aprobación de Idealista).
    idealista_api_client_id: str | None = None
    idealista_api_client_secret: str | None = None

    # --- Scraper propio (sin Apify) ---------------------------------------
    #: Mapa ``portal=proveedor``. ``local`` usa la fuente HTTP propia,
    #: ``idealista-api`` la Search API oficial (requiere acceso aprobado),
    #: ``apify`` delega en el actor configurado y ``none`` desactiva el portal.
    scraper_providers: str = "idealista=apify,fotocasa=local"
    #: Si una fuente propia falla o no devuelve nada, se reintenta con Apify.
    scraper_fallback_to_apify: bool = True
    scraper_max_items: int = Field(default=40, ge=1, le=200)
    scraper_max_pages: int = Field(default=5, ge=1, le=20)
    scraper_timeout_seconds: float = Field(default=20.0, gt=0)
    scraper_max_retries: int = Field(default=2, ge=0, le=5)
    scraper_rate_limit_per_minute: int = Field(default=20, ge=1, le=120)
    #: Vacío = UA de navegador por defecto (ver ``sources/http.py``).
    scraper_user_agent: str = ""
    #: Egreso alternativo para el scraper propio (proxy residencial, relay...).
    scraper_proxy_url: str | None = None
    #: Segmento de ubicación de Fotocasa cuando el texto libre no basta.
    scraper_fotocasa_location_segment: str | None = None

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
    #: Términos deseables del cron, separados por comas (no excluyentes).
    daily_preferred_terms: str | None = None
    #: Tipos de vivienda del cron, separados por comas (vacío = todos).
    daily_home_types: str | None = None
    #: Radio geográfico del cron (CP y radio deben configurarse juntos).
    daily_postal_code: str | None = None
    daily_radius_km: int | None = Field(default=None, ge=1, le=50)
    daily_min_year_built: int | None = Field(default=None, ge=1800, le=2100)
    #: Certificado energético mínimo del cron ("B" = B o mejor; vacío = sin filtro).
    daily_max_energy_rating: str | None = Field(default=None, pattern=r"^[A-Ga-g]$")

    # --- Alertas push de viviendas nuevas (app ntfy: iPhone y Android) ------
    #: Con ``false`` el job diario funciona igual, pero sin enviar notificaciones.
    alerts_enabled: bool = False
    #: Instancia de ntfy; ``https://ntfy.sh`` sirve para uso personal.
    ntfy_base_url: str = "https://ntfy.sh"
    #: Topic al que se suscribe la app móvil (largo y privado).
    ntfy_topic: str = ""
    #: Token de acceso, solo si el topic está protegido en el servidor ntfy.
    ntfy_token: str | None = None
    #: 1 (mínima) a 5 (urgente); ntfy la muestra como prioridad de la notificación.
    ntfy_priority: int = Field(default=3, ge=1, le=5)
    #: URL que abre la notificación al tocarla (vacío = la primera vivienda nueva).
    alert_click_url: str | None = None

    relevance_threshold: float = Field(default=0.6, ge=0.0, le=1.0)
    # Directorio donde persistir los resultados (vacío = solo en memoria).
    results_dir: str = "data/results"
    #: Fichero del perfil editable desde la UI (vacío = junto a ``results_dir``).
    search_profile_file: str | None = None
    #: Perfil de búsqueda compartido (job diario + UI + ejecución bajo demanda).
    search_profile_json: str | None = DEFAULT_SEARCH_PROFILE_JSON

    @field_validator(
        "apify_max_charge_usd",
        "idealista_api_client_id",
        "idealista_api_client_secret",
        "scraper_proxy_url",
        "scraper_fotocasa_location_segment",
        "daily_max_price_eur",
        "daily_min_size_m2",
        "daily_min_rooms",
        "daily_preferences",
        "daily_must_have",
        "daily_preferred_terms",
        "daily_home_types",
        "daily_postal_code",
        "daily_radius_km",
        "daily_min_year_built",
        "daily_max_energy_rating",
        "ntfy_token",
        "alert_click_url",
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

    @field_validator("daily_max_energy_rating", mode="after")
    @classmethod
    def _upper_energy_rating(cls, value: str | None) -> str | None:
        return value.upper() if value else value

    @field_validator("ntfy_topic")
    @classmethod
    def _validate_ntfy_topic(cls, value: str) -> str:
        """Los topics de ntfy solo admiten ``[-_A-Za-z0-9]`` hasta 64 caracteres."""
        topic = (value or "").strip()
        if not topic:
            return topic
        if not re.fullmatch(r"[-_A-Za-z0-9]{1,64}", topic):
            raise ValueError(
                "NTFY_TOPIC solo admite letras, números, '-' y '_' (máx. 64)"
            )
        return topic

    @field_validator("geocoding_base_url", "ntfy_base_url")
    @classmethod
    def _check_service_base_url(cls, value: str, info: Any) -> str:
        """Ni el geocodificador ni ntfy pueden quedar sin URL."""
        stripped = value.strip()
        if not stripped:
            raise ValueError(f"{info.field_name.upper()} no puede estar vacío")
        return stripped

    @model_validator(mode="after")
    def _validate_alerts(self) -> "Settings":
        """Con alertas activas, el topic es obligatorio: mejor fallar al arrancar."""
        if self.alerts_enabled and not (self.ntfy_topic or "").strip():
            raise ConfigurationException(
                "ALERTS_ENABLED=true requiere NTFY_TOPIC",
                details={"missing": ["NTFY_TOPIC"]},
            )
        return self

    @model_validator(mode="after")
    def _validate_required_secrets(self) -> "Settings":
        """Falla al arrancar si faltan credenciales (salvo en entorno de test).

        ``APIFY_TOKEN`` solo es obligatorio si algún portal usa el proveedor
        ``apify``: el scraper propio no necesita credenciales de terceros.
        """
        if self.environment == "test":
            return self
        required = {
            env_name: attribute
            for env_name, attribute in REQUIRED_SECRETS.items()
            if env_name != "APIFY_TOKEN"
        }
        providers = self.scraper_providers_map
        if any(provider == "apify" for provider in providers.values()):
            required["APIFY_TOKEN"] = "apify_token"
        if providers.get(Portal.IDEALISTA) == "idealista-api":
            required["IDEALISTA_API_CLIENT_ID"] = "idealista_api_client_id"
            required["IDEALISTA_API_CLIENT_SECRET"] = "idealista_api_client_secret"
        missing = [
            env_name
            for env_name, attribute in required.items()
            if not str(getattr(self, attribute) or "").strip()
        ]
        if missing:
            raise ConfigurationException(
                "Faltan variables de entorno obligatorias: " + ", ".join(missing),
                details={"missing": missing},
            )
        return self

    @property
    def scraper_providers_map(self) -> dict[Portal, str]:
        """``SCRAPER_PROVIDERS`` como mapa portal -> proveedor.

        Los portales no listados quedan desactivados (``none``) en lugar de asumir
        un proveedor por sorpresa: el mapa es la fuente de verdad.

        Raises:
            ConfigurationException: si el formato, el portal o el proveedor no son
                válidos.
        """
        raw = (self.scraper_providers or "").strip()
        if not raw:
            raise ConfigurationException(
                "SCRAPER_PROVIDERS no puede estar vacío",
                details={"hint": "ejemplo: idealista=apify,fotocasa=local"},
            )
        providers: dict[Portal, str] = {}
        for chunk in raw.split(","):
            entry = chunk.strip()
            if not entry:
                continue
            name, separator, provider = entry.partition("=")
            if not separator:
                raise ConfigurationException(
                    f"SCRAPER_PROVIDERS contiene una entrada sin '=': {entry!r}",
                    details={"invalid_entry": entry},
                )
            try:
                portal = Portal.parse(name)
            except ValueError as exc:
                raise ConfigurationException(
                    f"SCRAPER_PROVIDERS contiene un portal no soportado: {name!r}",
                    details={"invalid_portal": name.strip()},
                ) from exc
            normalized = provider.strip().lower()
            if normalized not in ("local", "apify", "idealista-api", "none"):
                raise ConfigurationException(
                    f"SCRAPER_PROVIDERS contiene un proveedor no soportado: {provider!r}",
                    details={"invalid_provider": provider.strip(), "portal": portal.value},
                )
            providers[portal] = normalized
        if not providers:
            raise ConfigurationException(
                "SCRAPER_PROVIDERS no contiene ningún portal",
                details={"value": raw},
            )
        return providers

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
            preferred_terms=self._parse_daily_preferred_terms(),
            home_types=self._parse_daily_home_types(),
            postal_code=postal_code,
            radius_km=radius_km,
            min_year_built=self.daily_min_year_built,
            max_energy_rating=self.daily_max_energy_rating,
        )

    def _parse_daily_preferred_terms(self) -> tuple[str, ...]:
        """Convierte ``DAILY_PREFERRED_TERMS`` (coma) en términos no excluyentes."""
        return self._parse_daily_terms(self.daily_preferred_terms, "DAILY_PREFERRED_TERMS")

    def _parse_daily_home_types(self) -> tuple[str, ...]:
        """Convierte ``DAILY_HOME_TYPES`` (coma) en valores canónicos."""
        from app.domain.models import parse_home_type

        canonical: list[str] = []
        for term in self._parse_daily_terms(self.daily_home_types, "DAILY_HOME_TYPES"):
            value = parse_home_type(term)
            if value is None:
                raise ConfigurationException(
                    f"DAILY_HOME_TYPES contiene un tipo no soportado: {term!r}",
                    details={"invalid_home_type": term},
                )
            if value not in canonical:
                canonical.append(value)
        return tuple(canonical)

    def _parse_daily_must_have(self) -> tuple[str, ...]:
        """Convierte ``DAILY_MUST_HAVE`` (lista separada por comas) en requisitos.

        Raises:
            ConfigurationException: si algún término no cumple el contrato de longitud
                (2-40 caracteres) que también aplica la API.
        """
        return self._parse_daily_terms(self.daily_must_have, "DAILY_MUST_HAVE")

    @staticmethod
    def _parse_daily_terms(raw: str | None, variable: str) -> tuple[str, ...]:
        """Lista separada por comas; valida longitud y deduplica sin distinguir caja."""
        terms: list[str] = []
        seen: set[str] = set()
        for raw_term in (raw or "").split(","):
            term = raw_term.strip()
            if not term:
                continue
            if not 2 <= len(term) <= 40:
                raise ConfigurationException(
                    f"{variable} contiene un término con longitud inválida "
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
