"""Tests del logging estructurado y de la configuración."""

from __future__ import annotations

import json
import logging

import pytest
import structlog

from app.config import Settings
from app.domain.exceptions import ConfigurationException
from app.domain.models import Portal
from app.infrastructure.logging_config import (
    REQUIRED_LOG_FIELDS,
    build_processors,
    configure_logging,
    ensure_required_fields,
)


def test_ensure_required_fields_fills_all_mandatory_keys() -> None:
    event = ensure_required_fields(None, "info", {"event": "search_started"})

    assert set(REQUIRED_LOG_FIELDS) <= set(event)
    assert event["search_id"] is None
    assert event["duration_ms"] is None


def test_ensure_required_fields_preserves_existing_values() -> None:
    event = ensure_required_fields(None, "info", {"event": "x", "search_id": "abc"})

    assert event["search_id"] == "abc"


def test_build_processors_uses_json_renderer_for_production() -> None:
    processors = build_processors(json_output=True)

    assert isinstance(processors[-1], structlog.processors.JSONRenderer)


def test_build_processors_uses_console_renderer_for_development() -> None:
    processors = build_processors(json_output=False)

    assert isinstance(processors[-1], structlog.dev.ConsoleRenderer)


def test_configure_logging_emits_json_with_mandatory_fields(capsys: pytest.CaptureFixture[str]) -> None:
    settings = Settings(
        environment="production",
        log_level="INFO",
        apify_token="token",
        deepseek_api_key="key",
    )
    configure_logging(settings, force=True)

    structlog.get_logger("test.json.renderer").info("search_started", search_id="s-1")
    output = [line for line in capsys.readouterr().out.strip().splitlines() if line.startswith("{")]

    assert output, "no se ha emitido ninguna línea JSON"
    payload = json.loads(output[-1])
    assert payload["event"] == "search_started"
    assert set(REQUIRED_LOG_FIELDS) <= set(payload)
    assert payload["level"] == "info"


def test_production_requires_credentials() -> None:
    with pytest.raises(ConfigurationException) as exc_info:
        Settings(environment="production", apify_token="", deepseek_api_key="")

    assert exc_info.value.code == "CONFIGURATION_ERROR"
    assert "missing" in exc_info.value.details


def test_production_accepts_present_credentials() -> None:
    settings = Settings(environment="production", apify_token="token", deepseek_api_key="key")

    assert settings.json_logs is True


def test_test_environment_skips_credential_validation() -> None:
    settings = Settings(environment="test")

    assert settings.json_logs is False


def test_build_daily_criteria_from_settings() -> None:
    settings = Settings(
        environment="test",
        search_profile_json="",
        daily_search_portals="idealista, fotocasa",
        daily_max_price_eur=1200.0,
        daily_min_rooms=2,
        daily_preferences="  con ascensor  ",
    )

    criteria = settings.build_daily_criteria()

    assert criteria.portals == (Portal.IDEALISTA, Portal.FOTOCASA)
    assert criteria.max_price_eur == 1200.0
    assert criteria.preferences == "con ascensor"


def test_build_daily_criteria_rejects_unknown_portal() -> None:
    settings = Settings(
        environment="test",
        search_profile_json="",
        daily_search_portals="pisos.com",
    )

    with pytest.raises(ConfigurationException):
        settings.build_daily_criteria()


def test_invalid_log_level_is_reported_as_configuration_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.config import get_settings

    monkeypatch.setenv("ENVIRONMENT", "test")
    monkeypatch.setenv("LOG_LEVEL", "LOUD")
    get_settings.cache_clear()
    try:
        with pytest.raises(ConfigurationException):
            get_settings()
    finally:
        get_settings.cache_clear()


def test_default_log_level_is_info() -> None:
    assert logging.INFO == getattr(logging, Settings(environment="test").log_level)
