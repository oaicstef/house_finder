"""Composición de canales de alerta del contenedor de servicios."""

from __future__ import annotations

from typing import Any

import app.api.dependencies as dependencies
from app.config import Settings
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
