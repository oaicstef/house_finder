"""Cliente HTTP compartido por las fuentes propias.

Aísla todo lo que un portal espera de un cliente educado: cabeceras de navegador,
timeout, reintentos con backoff en 429/5xx, respeto de ``Retry-After`` y un límite
de peticiones por minuto. Cualquier fallo definitivo se traduce a
``ScraperException`` para que el servicio de búsqueda no vea ``httpx``.
"""

from __future__ import annotations

import asyncio
import time
from datetime import timezone
from email.utils import parsedate_to_datetime
from typing import Any, Mapping

import httpx

from app.domain.exceptions import ScraperException
from app.infrastructure.logging_config import get_logger

__all__ = ["DEFAULT_USER_AGENT", "HttpClient"]

#: UA de un navegador real: los portales rechazan clientes que se identifican como bot.
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

#: Códigos que merecen un reintento (bloqueo temporal o error del portal).
_RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})


class HttpClient:
    """Cliente HTTP asíncrono con reintentos y límite de peticiones."""

    def __init__(
        self,
        *,
        user_agent: str = DEFAULT_USER_AGENT,
        timeout_seconds: float = 20.0,
        max_retries: int = 2,
        backoff_seconds: float = 1.0,
        rate_limit_per_minute: int = 20,
        proxy_url: str | None = None,
        logger: Any | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._logger = logger or get_logger("app.scraper")
        self._max_retries = max(0, max_retries)
        self._backoff_seconds = max(0.0, backoff_seconds)
        self._min_interval = 60.0 / rate_limit_per_minute if rate_limit_per_minute > 0 else 0.0
        self._throttle_lock = asyncio.Lock()
        self._last_request = 0.0
        self._client = httpx.AsyncClient(
            timeout=timeout_seconds,
            follow_redirects=True,
            transport=transport,
            proxy=proxy_url or None,
            headers={
                "User-Agent": user_agent,
                "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
                "Accept-Language": "es-ES,es;q=0.9",
                "Cache-Control": "no-cache",
            },
        )

    async def get(
        self,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> httpx.Response:
        """GET con reintentos; lanza ``ScraperException`` si no hay respuesta útil."""
        last_status: int | None = None
        last_error: str | None = None

        for attempt in range(self._max_retries + 1):
            await self._throttle()
            try:
                response = await self._client.get(url, params=params, headers=headers)
            except httpx.HTTPError as exc:
                last_status = None
                last_error = type(exc).__name__
                delay = self._backoff(attempt)
            else:
                if response.status_code < 400:
                    return response
                last_status = response.status_code
                last_error = None
                if response.status_code not in _RETRYABLE_STATUS or attempt >= self._max_retries:
                    raise ScraperException(
                        f"El portal respondió {response.status_code}",
                        details={
                            "url": str(response.request.url),
                            "status_code": response.status_code,
                        },
                    )
                delay = self._retry_after(response) or self._backoff(attempt)

            self._logger.warning(
                "scraper_http_retry",
                url=url,
                attempt=attempt + 1,
                status_code=last_status,
                error_type=last_error,
            )
            if attempt < self._max_retries:
                await asyncio.sleep(delay)

        raise ScraperException(
            "No se pudo contactar con el portal",
            details={"url": url, "status_code": last_status, "error_type": last_error},
        )

    async def post_form(
        self,
        url: str,
        *,
        data: Mapping[str, Any],
        auth: httpx.Auth | None = None,
    ) -> httpx.Response:
        """POST ``application/x-www-form-urlencoded`` (OAuth de la API oficial)."""
        await self._throttle()
        try:
            response = await self._client.post(url, data=data, auth=auth)
        except httpx.HTTPError as exc:
            raise ScraperException(
                "No se pudo contactar con el endpoint del portal",
                details={"url": url, "error_type": type(exc).__name__},
            ) from exc
        if response.status_code >= 400:
            raise ScraperException(
                f"El endpoint del portal respondió {response.status_code}",
                details={"url": str(response.request.url), "status_code": response.status_code},
            )
        return response

    async def aclose(self) -> None:
        await self._client.aclose()

    # ------------------------------------------------------------- helpers ---

    async def _throttle(self) -> None:
        """Serializa las peticiones para no pasar del ritmo configurado."""
        if not self._min_interval:
            return
        async with self._throttle_lock:
            wait = self._min_interval - (time.monotonic() - self._last_request)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_request = time.monotonic()

    def _backoff(self, attempt: int) -> float:
        return self._backoff_seconds * (2**attempt)

    @staticmethod
    def _retry_after(response: httpx.Response) -> float | None:
        """Segundos que pide el portal (``Retry-After`` numérico o fecha HTTP)."""
        raw = response.headers.get("retry-after")
        if not raw:
            return None
        try:
            return max(0.0, float(raw.strip()))
        except ValueError:
            pass
        try:
            when = parsedate_to_datetime(raw)
        except (TypeError, ValueError):
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        return max(0.0, when.timestamp() - time.time())
