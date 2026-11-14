"""Notificador push vía ntfy (app móvil para iPhone y Android).

``POST https://ntfy.sh/<topic>`` con el mensaje en el cuerpo y el título en la
cabecera ``Title``; la app suscrita al topic recibe la notificación al instante.
Las cabeceras HTTP solo admiten ASCII, así que el título se translitera.
"""

from __future__ import annotations

import unicodedata
from typing import Any
from urllib.parse import quote

import httpx

from app.domain.exceptions import NotificationException
from app.infrastructure.logging_config import get_logger

__all__ = ["NtfyNotifier"]

#: Host público por defecto; se puede apuntar a una instancia propia.
DEFAULT_BASE_URL = "https://ntfy.sh"


#: Puntuación tipográfica que NFKD eliminaría pegando palabras ("2 viviendas—Madrid").
_PUNCTUATION = str.maketrans(
    {
        "\u2014": "-",
        "\u2013": "-",
        "\u2018": "'",
        "\u2019": "'",
        "\u201c": '"',
        "\u201d": '"',
        "\u2026": "...",
        "\u00b7": "-",
    }
)


def _ascii_header(text: str) -> str:
    """Cabecera HTTP segura: sin acentos, sin saltos de línea y sin perder guiones."""
    translated = text.translate(_PUNCTUATION)
    decomposed = unicodedata.normalize("NFKD", translated)
    stripped = decomposed.encode("ascii", "ignore").decode("ascii")
    return " ".join(stripped.split()) or "House Finder"


class NtfyNotifier:
    """Implementación del puerto ``AlertNotifierPort`` sobre ntfy."""

    def __init__(
        self,
        topic: str,
        *,
        base_url: str = DEFAULT_BASE_URL,
        token: str | None = None,
        priority: int = 3,
        timeout_seconds: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
        logger: Any | None = None,
    ) -> None:
        self._topic = (topic or "").strip()
        self._base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self._token = (token or "").strip() or None
        self._priority = priority
        self._timeout = timeout_seconds
        self._transport = transport
        self._logger = logger or get_logger("app.alerts")
        if not self._topic:
            raise NotificationException(
                "NTFY_TOPIC es obligatorio para enviar alertas push",
                details={"hint": "instala la app ntfy y elige un topic privado"},
            )

    @property
    def url(self) -> str:
        return f"{self._base_url}/{quote(self._topic, safe='')}"

    async def send_digest(
        self,
        *,
        title: str,
        message: str,
        click_url: str | None = None,
    ) -> None:
        headers = {
            "Title": _ascii_header(title),
            "Priority": str(self._priority),
            "Tags": "house",
            "Content-Type": "text/plain; charset=utf-8",
        }
        if click_url:
            headers["Click"] = click_url
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"

        try:
            async with httpx.AsyncClient(
                timeout=self._timeout,
                transport=self._transport,
            ) as client:
                response = await client.post(
                    self.url,
                    content=message.encode("utf-8"),
                    headers=headers,
                )
        except httpx.HTTPError as exc:
            self._logger.warning(
                "alert_delivery_failed",
                error_code="NOTIFICATION_FAILURE",
                reason=type(exc).__name__,
            )
            raise NotificationException(
                "No se ha podido contactar con el servicio de notificaciones",
                details={"service": "ntfy"},
            ) from exc

        if response.status_code >= 400:
            self._logger.warning(
                "alert_delivery_failed",
                error_code="NOTIFICATION_FAILURE",
                status_code=response.status_code,
            )
            raise NotificationException(
                "El servicio de notificaciones ha rechazado el mensaje",
                details={"service": "ntfy", "status_code": response.status_code},
            )

        self._logger.info("alert_delivered", service="ntfy", status_code=response.status_code)
