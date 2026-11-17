"""Persistencia del estado del digest inicial de correo."""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from pathlib import Path

from app.domain.exceptions import RepositoryException

__all__ = ["InMemoryEmailNotificationState", "JsonEmailNotificationState"]

_STATE_KEY = "initial_digest_sent"


class InMemoryEmailNotificationState:
    """Estado del digest inicial para ejecuciones sin almacenamiento duradero."""

    def __init__(self) -> None:
        self._sent = False
        self._lock = asyncio.Lock()

    async def has_sent_initial_digest(self) -> bool:
        async with self._lock:
            return self._sent

    async def mark_initial_digest_sent(self) -> None:
        async with self._lock:
            self._sent = True


class JsonEmailNotificationState:
    """Estado atómico en disco, persistente junto al volumen de resultados."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._lock = asyncio.Lock()

    async def has_sent_initial_digest(self) -> bool:
        async with self._lock:
            return await asyncio.to_thread(self._read)

    async def mark_initial_digest_sent(self) -> None:
        async with self._lock:
            try:
                await asyncio.to_thread(self._write_atomic)
            except OSError as exc:
                raise self._repository_error("guardar") from exc

    def _read(self) -> bool:
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return False
        except (OSError, ValueError) as exc:
            raise self._repository_error("leer") from exc

        if not isinstance(payload, dict) or type(payload.get(_STATE_KEY)) is not bool:
            raise self._repository_error("validar")
        return payload[_STATE_KEY]

    def _write_atomic(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=str(self._path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump({_STATE_KEY: True}, handle)
            os.replace(tmp_name, self._path)
        except BaseException:
            Path(tmp_name).unlink(missing_ok=True)
            raise

    def _repository_error(self, operation: str) -> RepositoryException:
        return RepositoryException(
            "No se ha podido acceder al estado del email inicial",
            details={"file": self._path.name, "operation": operation},
        )
