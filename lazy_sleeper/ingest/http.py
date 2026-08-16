"""Thin HTTP client with retry/backoff. Injected into every source client."""

from __future__ import annotations

import logging
import time
from collections.abc import Mapping

import httpx

log = logging.getLogger(__name__)

RETRYABLE = {408, 425, 429, 500, 502, 503, 504}


class HttpClient:
    def __init__(
        self,
        *,
        timeout_s: float = 60.0,
        retries: int = 3,
        delay_ms: int = 250,
        user_agent: str = "lazy-sleeper/0.1 (+https://github.com/tkforgeworks/lazy-sleeper)",
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._retries = retries
        self._delay_s = delay_ms / 1000.0
        self._client = httpx.Client(
            timeout=timeout_s,
            headers={"User-Agent": user_agent, "Accept": "application/json, */*"},
            follow_redirects=True,
            transport=transport,
        )

    def get_bytes(
        self,
        url: str,
        *,
        params: Mapping[str, object] | list[tuple[str, object]] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> bytes:
        attempt = 0
        while True:
            attempt += 1
            try:
                resp = self._client.get(url, params=params, headers=headers)
                if resp.status_code in RETRYABLE and attempt <= self._retries:
                    raise httpx.HTTPStatusError("retryable", request=resp.request, response=resp)
                resp.raise_for_status()
                if self._delay_s:
                    time.sleep(self._delay_s)
                return resp.content
            except (httpx.TransportError, httpx.HTTPStatusError) as exc:
                if attempt > self._retries:
                    raise
                backoff = min(2 ** (attempt - 1), 8)
                log.warning(
                    "GET %s failed (%s); retry %d/%d in %ss",
                    url,
                    exc,
                    attempt,
                    self._retries,
                    backoff,
                )
                time.sleep(backoff)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> HttpClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
