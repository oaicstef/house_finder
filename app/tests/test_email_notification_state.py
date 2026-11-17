"""Tests de persistencia del primer digest SMTP."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.domain.exceptions import RepositoryException
from app.infrastructure.alerts.email_notification_state import (
    InMemoryEmailNotificationState,
    JsonEmailNotificationState,
)


async def test_json_state_survives_recreating_the_adapter(tmp_path: Path) -> None:
    path = tmp_path / "email-notification-state.json"
    state = JsonEmailNotificationState(path)

    assert not await state.has_sent_initial_digest()
    await state.mark_initial_digest_sent()

    reopened = JsonEmailNotificationState(path)
    assert await reopened.has_sent_initial_digest()
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "initial_digest_sent": True
    }


async def test_in_memory_state_starts_unsent_and_can_be_marked() -> None:
    state = InMemoryEmailNotificationState()

    assert not await state.has_sent_initial_digest()
    await state.mark_initial_digest_sent()
    assert await state.has_sent_initial_digest()


async def test_invalid_json_state_fails_explicitly(tmp_path: Path) -> None:
    path = tmp_path / "email-notification-state.json"
    path.write_text('{"initial_digest_sent": "true"}', encoding="utf-8")
    state = JsonEmailNotificationState(path)

    with pytest.raises(RepositoryException):
        await state.has_sent_initial_digest()
