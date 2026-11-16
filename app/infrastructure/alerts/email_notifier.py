"""Notificador de viviendas nuevas mediante un servidor SMTP."""

from __future__ import annotations

import asyncio
import smtplib
import ssl
from email.message import EmailMessage
from typing import Any, Callable, Sequence

from app.domain.exceptions import NotificationException
from app.infrastructure.logging_config import get_logger

__all__ = ["EmailNotifier"]


class EmailNotifier:
    """Entrega el resumen de viviendas nuevas por correo de texto plano."""

    channel = "email"

    def __init__(
        self,
        *,
        host: str,
        sender: str,
        recipients: Sequence[str],
        port: int = 587,
        username: str | None = None,
        password: str | None = None,
        starttls: bool = True,
        timeout_seconds: float = 15.0,
        smtp_factory: Callable[..., smtplib.SMTP] | None = None,
        logger: Any | None = None,
    ) -> None:
        self._host = host.strip()
        self._port = port
        self._sender = sender.strip()
        self._recipients = tuple(
            dict.fromkeys(address.strip() for address in recipients)
        )
        self._username = (username or "").strip() or None
        self._password = password if password and password.strip() else None
        self._starttls = starttls
        self._timeout = timeout_seconds
        self._smtp_factory = smtp_factory or smtplib.SMTP
        self._logger = logger or get_logger("app.alerts.email")
        if not self._host or not self._sender or not self._recipients:
            raise NotificationException(
                "La configuración SMTP requiere servidor, remitente y destinatarios",
                details={"service": "smtp"},
            )
        if bool(self._username) != bool(self._password):
            raise NotificationException(
                "Las credenciales SMTP deben configurarse juntas",
                details={"service": "smtp"},
            )

    async def send_digest(
        self,
        *,
        title: str,
        message: str,
        click_url: str | None = None,
    ) -> None:
        """Envía un email sin bloquear el event loop de la aplicación."""
        email = EmailMessage()
        email["Subject"] = " ".join(title.split())
        email["From"] = self._sender
        email["To"] = ", ".join(self._recipients)
        body = message
        if click_url:
            body += f"\n\nAbrir House Finder: {click_url}"
        email.set_content(body)

        try:
            await asyncio.to_thread(self._deliver, email)
        except (OSError, smtplib.SMTPException) as exc:
            self._logger.warning(
                "email_alert_delivery_failed",
                error_code="NOTIFICATION_FAILURE",
                reason=type(exc).__name__,
            )
            raise NotificationException(
                "No se ha podido entregar el correo de alerta",
                details={"service": "smtp"},
            ) from None

        self._logger.info(
            "email_alert_delivered",
            service="smtp",
            recipient_count=len(self._recipients),
        )

    def _deliver(self, email: EmailMessage) -> None:
        with self._smtp_factory(self._host, self._port, timeout=self._timeout) as smtp:
            smtp.ehlo()
            if self._starttls:
                smtp.starttls(context=ssl.create_default_context())
                smtp.ehlo()
            if self._username:
                smtp.login(self._username, self._password)
            refused = smtp.send_message(email)
            if refused:
                raise smtplib.SMTPRecipientsRefused(refused)
