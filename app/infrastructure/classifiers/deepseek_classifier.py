"""Adaptador de clasificación basado en DeepSeek (API compatible con OpenAI).

Implementa ``ClassifierPort``: envía los criterios y los anuncios en una sola llamada
en batch y exige una respuesta JSON estricta. Cualquier fallo -- red, cuota o payload
no procesable -- se traduce a ``ClassifierException``.
"""

from __future__ import annotations

import json
import re
from types import ModuleType
from typing import Any, Sequence

from app.domain.exceptions import ClassifierException
from app.domain.models import Listing, ListingAssessment, SearchCriteria
from app.infrastructure.logging_config import get_logger

__all__ = ["DeepSeekListingClassifier"]

_MODEL_NOT_CONFIGURED = "El clasificador no tiene un cliente configurado"

_SYSTEM_PROMPT = (
    "Eres un experto en evaluación de anuncios inmobiliarios en España. "
    "Recibirás unos criterios de búsqueda y una lista de anuncios. "
    "Devuelve EXCLUSIVAMENTE un objeto JSON con la forma:\n"
    '{"assessments": [{"listing_url": "<url exacta del anuncio>", '
    '"relevance": <número entre 0 y 1>, "matches": <true|false>, '
    '"reason": "<motivo breve en español>"}]}\n'
    "Reglas: usa exactamente las URLs recibidas (no inventes ninguna), "
    "evalúa todos los anuncios, penaliza lo que incumpla los criterios duros del "
    "usuario y puntúa 0.8 o más solo si el anuncio encaja claramente. "
    "No añadas texto fuera del JSON."
)

_JSON_FENCE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


class DeepSeekListingClassifier:
    """Clasificador LLM con cliente inyectable (facilita los tests)."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str = "deepseek-flash",
        base_url: str = "https://api.deepseek.com",
        timeout_seconds: float = 60.0,
        relevance_threshold: float = 0.6,
        client: Any | None = None,
        logger: Any | None = None,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._base_url = base_url
        self._timeout_seconds = timeout_seconds
        self._relevance_threshold = relevance_threshold
        self._client = client
        self._logger = logger or get_logger("app.classifier")

    async def assess_listings(
        self,
        criteria: SearchCriteria,
        listings: Sequence[Listing],
    ) -> list[ListingAssessment]:
        if not listings:
            return []

        client = self._client or self._build_client()
        payload = {
            "criteria": {
                "location": criteria.location,
                "min_price_eur": criteria.min_price_eur,
                "max_price_eur": criteria.max_price_eur,
                "min_size_m2": criteria.min_size_m2,
                "min_rooms": criteria.min_rooms,
                "preferences": criteria.preferences,
            },
            "listings": [
                {
                    "listing_url": listing.url,
                    "portal": listing.portal.value,
                    "title": listing.title,
                    "price_eur": listing.price_eur,
                    "size_m2": listing.size_m2,
                    "rooms": listing.rooms,
                    "bathrooms": listing.bathrooms,
                    "location": listing.location,
                    "price_per_m2": listing.price_per_m2,
                    "description": (listing.description or "")[:600],
                }
                for listing in listings
            ],
        }

        response = await self._request_assessments(client, payload)
        return self._parse_assessments(response, listings)

    async def _request_assessments(self, client: Any, payload: dict[str, Any]) -> str:
        try:
            completion = await client.chat.completions.create(
                model=self._model,
                messages=[
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
                ],
                response_format={"type": "json_object"},
                temperature=0.0,
                timeout=self._timeout_seconds,
            )
        except ClassifierException:
            raise
        except Exception as exc:  # frontera con el proveedor LLM: se traduce a dominio
            raise ClassifierException(
                "El proveedor LLM ha devuelto un error",
                details={"model": self._model, "error_type": type(exc).__name__},
            ) from exc

        try:
            content = completion.choices[0].message.content or ""
        except (AttributeError, IndexError, KeyError, TypeError) as exc:
            raise ClassifierException(
                "Respuesta del LLM con formato inesperado",
                details={"error_type": type(exc).__name__},
            ) from exc

        if not content.strip():
            raise ClassifierException("El LLM ha devuelto una respuesta vacía")
        return content

    def _parse_assessments(
        self,
        content: str,
        listings: Sequence[Listing],
    ) -> list[ListingAssessment]:
        data = self._load_json(content)
        raw_assessments = data.get("assessments")
        if not isinstance(raw_assessments, list):
            raise ClassifierException(
                "La respuesta del LLM no contiene la clave 'assessments'",
                details={"received_keys": sorted(data.keys())},
            )

        known_urls = {listing.url for listing in listings}
        assessments: list[ListingAssessment] = []
        for raw in raw_assessments:
            if not isinstance(raw, dict):
                continue
            url = raw.get("listing_url")
            if not isinstance(url, str) or url not in known_urls:
                # El modelo pudo alucinar una URL: se descarta silenciosamente.
                continue
            relevance = self._clamp(raw.get("relevance"))
            matches = raw.get("matches")
            if not isinstance(matches, bool):
                matches = relevance >= self._relevance_threshold
            reason = raw.get("reason")
            assessments.append(
                ListingAssessment(
                    listing_url=url,
                    relevance=relevance,
                    matches=matches,
                    reason=str(reason).strip() if reason else "Sin justificación del modelo",
                )
            )

        if not assessments:
            raise ClassifierException(
                "El LLM no ha devuelto ninguna evaluación válida para los anuncios enviados"
            )
        return assessments

    @staticmethod
    def _load_json(content: str) -> dict[str, Any]:
        candidate = content.strip()
        fence = _JSON_FENCE.search(candidate)
        if fence:
            candidate = fence.group(1)
        else:
            start, end = candidate.find("{"), candidate.rfind("}")
            if start == -1 or end <= start:
                raise ClassifierException(
                    "La respuesta del LLM no contiene un objeto JSON",
                )
            candidate = candidate[start : end + 1]

        try:
            data = json.loads(candidate)
        except json.JSONDecodeError as exc:
            raise ClassifierException(
                "La respuesta del LLM no es JSON válido",
                details={"error": exc.msg},
            ) from exc

        if not isinstance(data, dict):
            raise ClassifierException("La respuesta del LLM no es un objeto JSON")
        return data

    @staticmethod
    def _clamp(value: Any) -> float:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return 0.0
        return max(0.0, min(1.0, number))

    def _build_client(self) -> Any:
        openai_module = self._import_openai_module()
        if openai_module is None:  # pragma: no cover - depende del entorno
            raise ClassifierException(
                "El SDK de OpenAI no está instalado (openai)",
                details={"missing_package": "openai"},
            )
        if not self._api_key.strip():
            raise ClassifierException("Falta DEEPSEEK_API_KEY para el clasificador")
        return openai_module.AsyncOpenAI(
            api_key=self._api_key,
            base_url=self._base_url,
            timeout=self._timeout_seconds,
        )

    @staticmethod
    def _import_openai_module() -> ModuleType | None:
        """Import perezoso: el módulo se puede importar sin el SDK presente."""
        try:
            import openai
        except ImportError:  # pragma: no cover - depende del entorno
            return None
        return openai
