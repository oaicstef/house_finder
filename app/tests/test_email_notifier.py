"""Tests del notificador SMTP sin conexiones de red."""

from __future__ import annotations

import smtplib
from email.message import EmailMessage
from typing import Any

import pytest

from app.domain.exceptions import NotificationException
from app.infrastructure.alerts.email_notifier import EmailNotifier
from app.tests.conftest import RecordingLogger


class FakeSMTP:
    def __init__(
        self,
        *,
        refused: dict[str, tuple[int, bytes]] | None = None,
    ) -> None:
        self.refused = refused or {}
        self.calls: list[tuple[Any, ...]] = []
        self.message: EmailMessage | None = None

    def __enter__(self) -> FakeSMTP:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def ehlo(self) -> None:
        self.calls.append(("ehlo",))

    def starttls(self, *, context: Any) -> None:
        self.calls.append(("starttls", context))

    def login(self, username: str, password: str | None) -> None:
        self.calls.append(("login", username, password))

    def send_message(self, message: EmailMessage) -> dict[str, tuple[int, bytes]]:
        self.message = message
        self.calls.append(("send_message",))
        return self.refused


def build_notifier(
    smtp: FakeSMTP,
    *,
    logger: RecordingLogger | None = None,
) -> EmailNotifier:
    return EmailNotifier(
        host="smtp.example.com",
        sender="alerts@example.com",
        recipients=("one@example.com", "two@example.com"),
        username="mailer",
        password="smtp-password",
        smtp_factory=lambda *_args, **_kwargs: smtp,
        logger=logger or RecordingLogger(),
    )


async def test_sends_digest_over_starttls_with_authentication() -> None:
    smtp = FakeSMTP()
    notifier = build_notifier(smtp)

    await notifier.send_digest(
        title="2 viviendas nuevas — Madrid",
        message="1. Chalet\nhttps://homes.example/1",
        click_url="https://house-finder.example/",
    )

    assert smtp.message is not None
    assert smtp.message["Subject"] == "2 viviendas nuevas — Madrid"
    assert smtp.message["From"] == "alerts@example.com"
    assert smtp.message["To"] == "one@example.com, two@example.com"
    body = smtp.message.get_content()
    assert "https://homes.example/1" in body
    assert "Abrir House Finder: https://house-finder.example/" in body
    assert [call[0] for call in smtp.calls] == [
        "ehlo",
        "starttls",
        "ehlo",
        "login",
        "send_message",
    ]


async def test_sends_html_as_an_alternative_to_the_plain_text_body() -> None:
    smtp = FakeSMTP()
    notifier = build_notifier(smtp)

    await notifier.send_digest(
        title="Vivienda nueva",
        message="Casa\nhttps://homes.example/1",
        click_url="https://house-finder.example/",
        html_message="<p>Casa</p>",
    )

    assert smtp.message is not None
    assert smtp.message.get_content_type() == "multipart/alternative"
    text_part, html_part = smtp.message.iter_parts()
    assert text_part.get_content_type() == "text/plain"
    assert "https://homes.example/1" in text_part.get_content()
    assert html_part.get_content_type() == "text/html"
    assert "<p>Casa</p>" in html_part.get_content()
    assert "Abrir House Finder: https://house-finder.example/" in html_part.get_content()


async def test_rejected_recipient_becomes_notification_exception() -> None:
    refused = {"one@example.com": (550, b"mailbox unavailable")}
    smtp = FakeSMTP(refused=refused)
    notifier = build_notifier(smtp)

    with pytest.raises(NotificationException) as raised:
        await notifier.send_digest(title="Nuevas viviendas", message="Resumen")

    assert raised.value.code == "NOTIFICATION_FAILURE"


async def test_smtp_errors_become_notification_exception() -> None:
    def fail_factory(*_args: Any, **_kwargs: Any) -> FakeSMTP:
        raise smtplib.SMTPConnectError(421, b"service unavailable")

    notifier = EmailNotifier(
        host="smtp.example.com",
        sender="alerts@example.com",
        recipients=("one@example.com",),
        smtp_factory=fail_factory,
        logger=RecordingLogger(),
    )

    with pytest.raises(NotificationException) as raised:
        await notifier.send_digest(title="Nuevas viviendas", message="Resumen")

    assert raised.value.code == "NOTIFICATION_FAILURE"
