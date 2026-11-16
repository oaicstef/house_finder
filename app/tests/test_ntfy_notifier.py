"""Tests del notificador ntfy (sin red: transporte httpx falso)."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from app.domain.exceptions import NotificationException
from app.infrastructure.alerts.ntfy_notifier import NtfyNotifier
from app.tests.conftest import RecordingLogger


def make_notifier(handler: Any, **kwargs: Any) -> NtfyNotifier:
    return NtfyNotifier(
        kwargs.pop("topic", "mi-topic"),
        transport=httpx.MockTransport(handler),
        logger=RecordingLogger(),
        **kwargs,
    )


async def test_sends_the_digest_to_the_topic_endpoint() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["url"] = str(request.url)
        seen["body"] = request.content.decode("utf-8")
        seen["headers"] = dict(request.headers)
        return httpx.Response(200, json={"id": "abc"})

    notifier = make_notifier(handler)
    await notifier.send_digest(
        title="2 viviendas nuevas \u2014 Madrid",
        message="1. 895.000 \u20ac\nhttps://p/1",
        click_url="https://p/1",
    )

    assert seen["method"] == "POST"
    assert seen["url"] == "https://ntfy.sh/mi-topic"
    assert seen["body"] == "1. 895.000 \u20ac\nhttps://p/1"
    assert seen["headers"]["title"] == "2 viviendas nuevas - Madrid"
    assert seen["headers"]["click"] == "https://p/1"
    assert seen["headers"]["priority"] == "3"
    assert "authorization" not in seen["headers"], "sin token no se envía cabecera"


async def test_uses_the_configured_instance_topic_and_token() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["authorization"] = request.headers.get("authorization")
        return httpx.Response(200)

    notifier = make_notifier(
        handler,
        topic="casa-nueva",
        base_url="https://ntfy.example/",
        token="tk_123",
        priority=5,
    )
    await notifier.send_digest(title="Hola", message="texto")

    assert seen["url"] == "https://ntfy.example/casa-nueva"
    assert seen["authorization"] == "Bearer tk_123"


async def test_http_errors_become_notification_exceptions() -> None:
    notifier = make_notifier(lambda request: httpx.Response(500))

    with pytest.raises(NotificationException) as raised:
        await notifier.send_digest(title="Hola", message="texto")

    assert raised.value.code == "NOTIFICATION_FAILURE"
    assert raised.value.details["status_code"] == 500


async def test_network_errors_become_notification_exceptions() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("sin red", request=request)

    notifier = make_notifier(handler)

    with pytest.raises(NotificationException):
        await notifier.send_digest(title="Hola", message="texto")


def test_a_topic_is_required() -> None:
    with pytest.raises(NotificationException):
        NtfyNotifier("   ")
