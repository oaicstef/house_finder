"""Tests de la configuración tipada, en especial de los criterios del cron diario."""

from __future__ import annotations

import pytest

from app.config import Settings
from app.domain.exceptions import ConfigurationException


def build_settings(**overrides: object) -> Settings:
    """Configuración aislada del ``.env`` real del desarrollador.

    ``search_profile_json=""`` desactiva el perfil compartido para probar la ruta
    heredada ``DAILY_*``.
    """
    values: dict[str, object] = {
        "_env_file": None,
        "environment": "test",
        "search_profile_json": "",
    }
    values.update(overrides)
    return Settings(**values)


def test_build_daily_criteria_without_must_have_has_no_hard_filter() -> None:
    criteria = build_settings().build_daily_criteria()

    assert criteria.must_have == ()


def test_blank_daily_must_have_becomes_none() -> None:
    settings = build_settings(daily_must_have="   ")

    assert settings.daily_must_have is None
    assert settings.build_daily_criteria().must_have == ()


def test_daily_must_have_is_parsed_and_deduplicated() -> None:
    settings = build_settings(daily_must_have=" piscina, Padel ,piscina, ,")

    criteria = settings.build_daily_criteria()

    assert criteria.must_have == ("piscina", "Padel")


def test_daily_must_have_accepts_terms_with_spaces() -> None:
    settings = build_settings(daily_must_have="aire acondicionado, pista de padel")

    assert settings.build_daily_criteria().must_have == (
        "aire acondicionado",
        "pista de padel",
    )


@pytest.mark.parametrize("invalid_term", ["a", "x" * 41])
def test_invalid_daily_must_have_raises_configuration_exception(invalid_term: str) -> None:
    settings = build_settings(daily_must_have=f"piscina,{invalid_term}")

    with pytest.raises(ConfigurationException) as exc_info:
        settings.build_daily_criteria()

    assert exc_info.value.code == "CONFIGURATION_ERROR"
    assert exc_info.value.details["invalid_requirement"] == invalid_term


def test_default_actors_are_usable_or_empty() -> None:
    """Un default no puede ser un placeholder: un id inexistente devuelve 404.

    Se comprueba el valor declarado en el modelo y no ``Settings()``, porque ``.env``
    se carga en ``os.environ`` al importar ``app.config``.
    """
    idealista = Settings.model_fields["apify_idealista_actor"].default
    fotocasa = Settings.model_fields["apify_fotocasa_actor"].default

    assert idealista == "igolaizola/idealista-scraper"
    assert "/" in idealista
    # Fotocasa es pay-per-event: se omite salvo que el usuario lo active.
    assert fotocasa == ""


def test_daily_must_have_changes_the_criteria_fingerprint() -> None:
    without_requirements = build_settings().build_daily_criteria()
    with_requirements = build_settings(daily_must_have="piscina,padel").build_daily_criteria()

    assert without_requirements.fingerprint() != with_requirements.fingerprint()
