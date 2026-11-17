"""Composición de canales de alerta del contenedor de servicios."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import app.api.dependencies as dependencies
from app.config import Settings
from app.infrastructure.alerts.email_notification_state import (
    InMemoryEmailNotificationState,
    JsonEmailNotificationState,
)
from app.tests.conftest import FakeClassifier, FakeNotifier, FakeScraper


def test_build_container_composes_enabled_notification_channels(monkeypatch: Any) -> None:
    settings = Settings(
        _env_file=None,
        environment="test",
        results_dir="",
        search_profile_json="",
        alerts_enabled=True,
        ntfy_topic="test-topic",
        email_alerts_enabled=True,
        email_smtp_host="smtp.example.com",
        email_from="alerts@example.com",
        email_to="user@example.com",
    )
    monkeypatch.setattr(dependencies, "build_scraper", lambda _settings: FakeScraper())
    monkeypatch.setattr(
        dependencies,
        "DeepSeekListingClassifier",
        lambda **_kwargs: FakeClassifier(),
    )

    def fake_notifier(channel: str) -> FakeNotifier:
        notifier = FakeNotifier()
        notifier.channel = channel
        return notifier

    monkeypatch.setattr(
        dependencies,
        "NtfyNotifier",
        lambda *_args, **_kwargs: fake_notifier("ntfy"),
    )
    monkeypatch.setattr(
        dependencies,
        "EmailNotifier",
        lambda **_kwargs: fake_notifier("email"),
    )

    container = dependencies.build_container(settings)

    assert [notifier.channel for notifier in container.alert_notifiers] == [
        "ntfy",
        "email",
    ]
    assert isinstance(
        container.email_notification_state,
        InMemoryEmailNotificationState,
    )


async def test_build_container_persists_email_state_alongside_results(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    settings = Settings(
        _env_file=None,
        environment="test",
        results_dir=str(tmp_path / "results"),
        search_profile_json="",
        email_alerts_enabled=True,
        email_smtp_host="smtp.example.com",
        email_from="alerts@example.com",
        email_to="user@example.com",
    )
    monkeypatch.setattr(dependencies, "build_scraper", lambda _settings: FakeScraper())
    monkeypatch.setattr(
        dependencies,
        "DeepSeekListingClassifier",
        lambda **_kwargs: FakeClassifier(),
    )

    container = dependencies.build_container(settings)

    assert isinstance(container.email_notification_state, JsonEmailNotificationState)
    assert container.email_notification_state is not None
    await container.email_notification_state.mark_initial_digest_sent()
    assert (tmp_path / "email-notification-state.json").is_file()
