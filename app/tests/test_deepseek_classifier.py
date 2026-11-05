"""Tests del clasificador DeepSeek con un cliente LLM falso (sin red)."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from app.domain.exceptions import ClassifierException
from app.domain.models import Listing, Portal, SearchCriteria
from app.infrastructure.classifiers.deepseek_classifier import DeepSeekListingClassifier
from app.tests.conftest import RecordingLogger, make_listing


class _FakeCompletions:
    def __init__(self, content: str | None, error: Exception | None) -> None:
        self._content = content
        self._error = error
        self.requests: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:
        self.requests.append(kwargs)
        if self._error is not None:
            raise self._error
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=self._content))]
        )


class _FakeLLMClient:
    def __init__(self, content: str | None = None, error: Exception | None = None) -> None:
        self.completions = _FakeCompletions(content, error)
        self.chat = SimpleNamespace(completions=self.completions)


def build_classifier(client: _FakeLLMClient) -> DeepSeekListingClassifier:
    return DeepSeekListingClassifier(
        api_key="test-key",
        model="deepseek-flash",
        relevance_threshold=0.6,
        client=client,
        logger=RecordingLogger(),
    )


CRITERIA = SearchCriteria(location="Madrid", max_price_eur=1500.0)


async def test_parses_valid_json_and_drops_unknown_urls() -> None:
    listing = make_listing()
    payload = {
        "assessments": [
            {"listing_url": listing.url, "relevance": 0.87, "matches": True, "reason": "Encaja"},
            {"listing_url": "https://alucinado.test/1", "relevance": 0.9, "matches": True, "reason": "X"},
        ]
    }
    client = _FakeLLMClient(json.dumps(payload))

    assessments = await build_classifier(client).assess_listings(CRITERIA, [listing])

    assert len(assessments) == 1
    assert assessments[0].listing_url == listing.url
    assert assessments[0].relevance == 0.87
    assert assessments[0].matches is True


async def test_parses_json_wrapped_in_markdown_fence() -> None:
    listing = make_listing()
    payload = {"assessments": [{"listing_url": listing.url, "relevance": 0.5, "matches": False}]}
    client = _FakeLLMClient(f"```json\n{json.dumps(payload)}\n```")

    assessments = await build_classifier(client).assess_listings(CRITERIA, [listing])

    assert assessments[0].matches is False
    assert assessments[0].reason == "Sin justificación del modelo"


async def test_derives_matches_from_threshold_when_missing() -> None:
    listing = make_listing()
    payload = {"assessments": [{"listing_url": listing.url, "relevance": 0.95}]}
    client = _FakeLLMClient(json.dumps(payload))

    assessments = await build_classifier(client).assess_listings(CRITERIA, [listing])

    assert assessments[0].matches is True


async def test_clamps_relevance_to_unit_interval() -> None:
    listing = make_listing()
    payload = {"assessments": [{"listing_url": listing.url, "relevance": 42, "matches": True}]}
    client = _FakeLLMClient(json.dumps(payload))

    assessments = await build_classifier(client).assess_listings(CRITERIA, [listing])

    assert assessments[0].relevance == 1.0


async def test_threshold_overrides_llm_matches_flag() -> None:
    """El LLM solo puntúa: un `matches=true` con relevancia baja no se acepta."""
    listing = make_listing()
    payload = {
        "assessments": [
            {"listing_url": listing.url, "relevance": 0.3, "matches": True, "reason": "Dudoso"}
        ]
    }
    client = _FakeLLMClient(json.dumps(payload))

    assessments = await build_classifier(client).assess_listings(CRITERIA, [listing])

    assert assessments[0].matches is False


async def test_threshold_accepts_high_relevance_even_if_llm_says_no() -> None:
    """Un `matches=false` con relevancia alta sí se acepta (preferencia ausente)."""
    listing = make_listing()
    payload = {
        "assessments": [
            {
                "listing_url": listing.url,
                "relevance": 0.68,
                "matches": False,
                "reason": "Chalet con piscina; no consta pista de pádel",
            }
        ]
    }
    client = _FakeLLMClient(json.dumps(payload))

    assessments = await build_classifier(client).assess_listings(CRITERIA, [listing])

    assert assessments[0].matches is True


async def test_payload_includes_verified_requirements() -> None:
    """El LLM debe saber qué requisitos ya se comprobaron, para no "echarlos en falta"."""
    listing = Listing(
        portal=Portal.IDEALISTA,
        url="https://www.idealista.com/inmueble/1/",
        title="Chalet adosado en Pau de Carabanchel",
        price_eur=880000.0,
        location="Madrid",
        size_m2=225.0,
        rooms=5,
        description="Zona común con piscinas y pista pádel.",
        amenities=("swimming_pool",),
    )
    payload = {"assessments": [{"listing_url": listing.url, "relevance": 0.9}]}
    client = _FakeLLMClient(json.dumps(payload))
    criteria = SearchCriteria(location="Madrid", must_have=("padel", "piscina"))

    await build_classifier(client).assess_listings(criteria, [listing])

    sent = json.loads(client.completions.requests[0]["messages"][1]["content"])
    assert sent["listings"][0]["verified_requirements"] == ["padel", "piscina"]
    assert sent["criteria"]["must_have"] == ["padel", "piscina"]


async def test_sends_model_and_json_response_format() -> None:
    listing = make_listing()
    payload = {"assessments": [{"listing_url": listing.url, "relevance": 0.8, "matches": True}]}
    client = _FakeLLMClient(json.dumps(payload))

    await build_classifier(client).assess_listings(CRITERIA, [listing])

    request = client.completions.requests[0]
    assert request["model"] == "deepseek-flash"
    assert request["response_format"] == {"type": "json_object"}
    assert request["temperature"] == 0.0


async def test_returns_empty_without_calling_llm_when_no_listings() -> None:
    client = _FakeLLMClient("{}")

    assert await build_classifier(client).assess_listings(CRITERIA, []) == []
    assert client.completions.requests == []


async def test_raises_classifier_exception_on_invalid_json() -> None:
    client = _FakeLLMClient("esto no es json")

    with pytest.raises(ClassifierException) as exc_info:
        await build_classifier(client).assess_listings(CRITERIA, [make_listing()])

    assert exc_info.value.code == "CLASSIFIER_FAILURE"


async def test_raises_classifier_exception_when_key_is_missing() -> None:
    client = _FakeLLMClient(json.dumps({"resultado": []}))

    with pytest.raises(ClassifierException):
        await build_classifier(client).assess_listings(CRITERIA, [make_listing()])


async def test_raises_classifier_exception_when_provider_fails() -> None:
    client = _FakeLLMClient(error=RuntimeError("timeout"))

    with pytest.raises(ClassifierException) as exc_info:
        await build_classifier(client).assess_listings(CRITERIA, [make_listing()])

    assert exc_info.value.details["error_type"] == "RuntimeError"


async def test_raises_classifier_exception_on_empty_content() -> None:
    client = _FakeLLMClient(content="   ")

    with pytest.raises(ClassifierException):
        await build_classifier(client).assess_listings(CRITERIA, [make_listing()])
