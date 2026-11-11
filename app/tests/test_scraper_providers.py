"""Tests del mapa ``SCRAPER_PROVIDERS`` y de la fábrica de scrapers."""

from __future__ import annotations

from typing import Any

import pytest

from app.config import Settings
from app.domain.exceptions import ConfigurationException
from app.domain.models import Portal
from app.infrastructure.scrapers.apify_scraper import ApifyListingScraper
from app.infrastructure.scrapers.factory import build_scraper
from app.infrastructure.scrapers.listing_scraper import ListingScraper


def make_settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "environment": "test",
        "apify_token": "apify_api_test",
        "apify_idealista_actor": "actor-idealista",
        "apify_fotocasa_actor": "actor-fotocasa",
        "deepseek_api_key": "sk-test",
    }
    base.update(overrides)
    return Settings(**base)


def test_the_default_map_keeps_apify_for_idealista_and_uses_the_local_scraper_for_fotocasa() -> None:
    settings = make_settings()

    assert settings.scraper_providers_map == {
        Portal.IDEALISTA: "apify",
        Portal.FOTOCASA: "local",
    }


def test_none_disables_a_portal_and_unlisted_portals_are_absent() -> None:
    settings = make_settings(scraper_providers="fotocasa=local")

    assert settings.scraper_providers_map == {Portal.FOTOCASA: "local"}


def test_an_unknown_portal_is_rejected_at_startup() -> None:
    settings = make_settings(scraper_providers="idealista=apify,habitaclia=local")

    with pytest.raises(ConfigurationException) as exc_info:
        settings.scraper_providers_map

    assert exc_info.value.details["invalid_portal"] == "habitaclia"


def test_an_unknown_provider_is_rejected_at_startup() -> None:
    settings = make_settings(scraper_providers="fotocasa=selenium")

    with pytest.raises(ConfigurationException) as exc_info:
        settings.scraper_providers_map

    assert exc_info.value.details["invalid_provider"] == "selenium"


def test_an_entry_without_equals_is_rejected_at_startup() -> None:
    settings = make_settings(scraper_providers="fotocasa")

    with pytest.raises(ConfigurationException) as exc_info:
        settings.scraper_providers_map

    assert exc_info.value.details["invalid_entry"] == "fotocasa"


def test_a_local_provider_for_a_portal_without_implementation_is_rejected() -> None:
    settings = make_settings(scraper_providers="idealista=local,fotocasa=local")

    with pytest.raises(ConfigurationException) as exc_info:
        build_scraper(settings)

    assert exc_info.value.details["portal"] == "idealista"


def test_apify_token_is_required_only_when_a_portal_uses_apify() -> None:
    local_only = make_settings(
        environment="production",
        apify_token="",
        scraper_providers="fotocasa=local",
    )
    assert local_only.scraper_providers_map == {Portal.FOTOCASA: "local"}

    with pytest.raises(ConfigurationException) as exc_info:
        make_settings(
            environment="production",
            apify_token="",
            scraper_providers="idealista=apify,fotocasa=local",
        )
    assert "APIFY_TOKEN" in str(exc_info.value)


def test_all_apify_returns_the_apify_adapter() -> None:
    scraper = build_scraper(make_settings(scraper_providers="idealista=apify,fotocasa=apify"))

    assert isinstance(scraper, ApifyListingScraper)
    assert set(scraper.source_names) == {Portal.IDEALISTA, Portal.FOTOCASA}


def test_a_local_portal_returns_the_composite_with_apify_as_fallback() -> None:
    scraper = build_scraper(make_settings())

    assert isinstance(scraper, ListingScraper)
    assert scraper.source_names == {Portal.FOTOCASA: "fotocasa-http"}
    assert scraper.delegate_names == {
        Portal.IDEALISTA: "apify",
        Portal.FOTOCASA: "apify",
    }


def test_the_fallback_can_be_disabled_by_configuration() -> None:
    scraper = build_scraper(make_settings(scraper_fallback_to_apify=False))

    assert scraper.source_names == {Portal.FOTOCASA: "fotocasa-http"}
    assert scraper.delegate_names == {Portal.IDEALISTA: "apify"}


def test_without_apify_token_the_local_scraper_runs_without_fallback() -> None:
    scraper = build_scraper(
        make_settings(apify_token="", scraper_providers="fotocasa=local")
    )

    assert scraper.source_names == {Portal.FOTOCASA: "fotocasa-http"}
    assert scraper.delegate_names == {}


def test_official_idealista_provider_is_available_without_apify() -> None:
    scraper = build_scraper(
        make_settings(
            apify_token="",
            scraper_providers="idealista=idealista-api,fotocasa=local",
            idealista_api_client_id="approved-id",
            idealista_api_client_secret="approved-secret",
        )
    )

    assert isinstance(scraper, ListingScraper)
    assert scraper.source_names == {
        Portal.IDEALISTA: "idealista-official-api",
        Portal.FOTOCASA: "fotocasa-http",
    }
    assert scraper.delegate_names == {}


def test_official_idealista_provider_is_only_valid_for_idealista() -> None:
    with pytest.raises(ConfigurationException) as exc_info:
        build_scraper(make_settings(scraper_providers="fotocasa=idealista-api"))

    assert exc_info.value.details["portals"] == ["fotocasa"]


def test_official_idealista_credentials_are_required_in_production() -> None:
    with pytest.raises(ConfigurationException) as exc_info:
        Settings(
            _env_file=None,
            environment="production",
            deepseek_api_key="sk-test",
            apify_token="",
            scraper_providers="idealista=idealista-api",
        )

    assert exc_info.value.details["missing"] == [
        "IDEALISTA_API_CLIENT_ID",
        "IDEALISTA_API_CLIENT_SECRET",
    ]


def test_blank_official_idealista_credentials_are_normalized_to_none() -> None:
    settings = make_settings(
        idealista_api_client_id="",
        idealista_api_client_secret=" ",
    )

    assert settings.idealista_api_client_id is None
    assert settings.idealista_api_client_secret is None


def test_official_idealista_credentials_do_not_require_apify_in_production() -> None:
    settings = Settings(
        _env_file=None,
        environment="production",
        deepseek_api_key="sk-test",
        apify_token="",
        scraper_providers="idealista=idealista-api",
        idealista_api_client_id="approved-id",
        idealista_api_client_secret="approved-secret",
    )

    assert settings.idealista_api_client_id == "approved-id"
