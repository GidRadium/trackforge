"""One shared asynchronous HTTP client for metadata providers.

The client owns timeout, retry/backoff, redirect handling, SSRF validation,
and request pacing.  It returns a small immutable response object instead of
leaking ``httpx`` types into adapters and application code.

Security rules implemented here:

* only ``http``/``https`` URLs are accepted;
* every hop is checked by :class:`~trackforge.adapters.http.url_safety.UrlSafetyGuard`;
* redirects are followed manually with a hard limit;
* query strings containing secrets are redacted from errors and logs;
* ``offline`` mode fails before any socket is opened.
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import re
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urljoin

import httpx

from trackforge import __version__
from trackforge.adapters.http.rate_limit import HostRateLimiter, parse_retry_after
from trackforge.adapters.http.url_safety import (
    UrlSafetyGuard,
    redact_text,
    sanitize_url,
)
from trackforge.domain.errors import OfflineModeError, ProviderUnavailable, RateLimited, UnsafeUrl

_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})
_DEFAULT_TIMEOUT = 20.0
_DEFAULT_MAX_BYTES = 10 * 1024 * 1024
_USER_AGENT_ENV = "TRACKFORGE_MUSICBRAINZ_USER_AGENT"
_BODY_SECRET_RE = re.compile(
    r'(?i)("?(?:cookie|set-cookie|authorization|api[_-]?key|access[_-]?token|'
    r'refresh[_-]?token|token|secret|session(?:[_-]?id)?)"?\s*[:=]\s*)'
    r'("[^"]*"|[^,;}\s]+)'
)

Sleeper = Callable[[float], Awaitable[None]]


def build_user_agent(contact: str | None = None) -> str:
    """Build the mandatory provider User-Agent string."""

    raw = (contact if contact is not None else os.environ.get(_USER_AGENT_ENV, "")).strip()
    if not raw:
        return f"TrackForge/{__version__}"
    if raw.casefold().startswith("trackforge/") or " (" in raw:
        return raw
    return f"TrackForge/{__version__} ({raw})"


@dataclass(frozen=True, slots=True)
class HttpResponse:
    """A bounded, transport-neutral HTTP response."""

    status_code: int
    url: str
    headers: Mapping[str, str] = field(default_factory=dict)
    content: bytes = b""
    elapsed_ms: int = 0
    redirects: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 300

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", errors="replace")

    @property
    def content_type(self) -> str | None:
        for key, value in self.headers.items():
            if key.casefold() == "content-type":
                return str(value)
        return None

    def json(self) -> Any:
        """Decode the body as JSON, with a redacted error on failure."""

        try:
            return json.loads(self.content.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProviderUnavailable(
                "provider returned invalid JSON",
                retryable=False,
                user_action="Retry later or report the provider response.",
                context={"url": sanitize_url(self.url), "error": str(exc)},
            ) from exc

    def body_excerpt(self, max_chars: int = 512) -> str:
        """Return a redacted, bounded text excerpt of the response body.

        Headers, cookies, and URL query strings are intentionally excluded.
        Binary-looking bodies are summarized instead of decoded.
        """

        if not self.content:
            return ""
        content_type = (self.content_type or "").casefold()
        if content_type.startswith(("image/", "audio/", "video/", "application/octet-stream")):
            return f"<binary:{len(self.content)} bytes>"
        text = self.content.decode("utf-8", errors="replace")
        text = text.replace("\x00", " ")
        text = _BODY_SECRET_RE.sub(r'\1"***"', text)
        text = redact_text(text)
        text = " ".join(text.split())
        if max_chars < 1:
            return ""
        if len(text) > max_chars:
            text = f"{text[: max_chars - 1]}…"
        return text

    def raise_for_status(self) -> None:
        if self.ok:
            return
        context = {
            "url": sanitize_url(self.url),
            "status": self.status_code,
            "body_excerpt": self.body_excerpt(),
        }
        if self.status_code == 429:
            raise RateLimited(
                "provider rate-limited the request",
                context=context,
            )
        raise ProviderUnavailable(
            f"provider returned HTTP {self.status_code}",
            retryable=self.status_code >= 500,
            context=context,
        )


class HttpClient:
    """A single retrying/paced HTTP client used by all providers."""

    def __init__(
        self,
        *,
        user_agent: str | None = None,
        contact: str | None = None,
        timeout: float | httpx.Timeout = _DEFAULT_TIMEOUT,
        max_retries: int = 2,
        retry_backoff: float = 0.25,
        retry_jitter: float = 0.1,
        max_redirects: int = 5,
        limiter: HostRateLimiter | None = None,
        guard: UrlSafetyGuard | None = None,
        offline: bool = False,
        proxy: str | None = None,
        client: httpx.AsyncClient | None = None,
        sleep: Sleeper | None = None,
        rng: Callable[[], float] | None = None,
        max_bytes: int = _DEFAULT_MAX_BYTES,
    ) -> None:
        if max_retries < 0:
            raise ValueError("max_retries must not be negative")
        if max_redirects < 0:
            raise ValueError("max_redirects must not be negative")
        if retry_backoff < 0 or retry_jitter < 0:
            raise ValueError("backoff and jitter must not be negative")
        if max_bytes < 1:
            raise ValueError("max_bytes must be positive")
        self._offline = bool(offline)
        self._proxy = proxy.strip() if isinstance(proxy, str) and proxy.strip() else None
        self._contact = contact if contact is not None else os.environ.get(_USER_AGENT_ENV, "")
        self._user_agent = user_agent or build_user_agent(self._contact)
        self._timeout = timeout
        self._max_retries = max_retries
        self._retry_backoff = retry_backoff
        self._retry_jitter = retry_jitter
        self._max_redirects = max_redirects
        self._limiter = limiter or HostRateLimiter()
        self._guard = guard or UrlSafetyGuard(max_redirects=max_redirects)
        self._sleep: Sleeper | None = sleep
        self._rng = rng or random.random
        self._max_bytes = max_bytes
        self._owned = client is None
        self._client = client or httpx.AsyncClient(
            timeout=self._timeout,
            follow_redirects=False,
            cookies=None,
            trust_env=False,
            proxy=self._proxy,
            headers={"User-Agent": self._user_agent, "Accept": "application/json"},
        )
        self._client.follow_redirects = False

    @property
    def user_agent(self) -> str:
        return self._user_agent

    @property
    def contact_configured(self) -> bool:
        return bool(self._contact.strip())

    @property
    def offline(self) -> bool:
        return self._offline

    @property
    def proxy_configured(self) -> bool:
        return self._proxy is not None

    @property
    def limiter(self) -> HostRateLimiter:
        return self._limiter

    async def __aenter__(self) -> HttpClient:
        return self

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owned:
            await self._client.aclose()

    async def get(
        self,
        url: str,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        timeout: float | httpx.Timeout | None = None,
    ) -> HttpResponse:
        """Perform a bounded GET with retries and SSRF-checked redirects."""

        return await self._request(
            "GET",
            url,
            params=params,
            headers=headers,
            timeout=timeout,
        )

    async def post(
        self,
        url: str,
        data: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        timeout: float | httpx.Timeout | None = None,
        *,
        content: bytes | None = None,
        json: Any = None,
        json_body: Any = None,
    ) -> HttpResponse:
        """Perform a bounded POST with retries and SSRF-checked redirects."""

        return await self._request(
            "POST",
            url,
            data=data,
            content=content,
            json_body=json if json is not None else json_body,
            headers=headers,
            timeout=timeout,
        )

    async def get_bytes(self, url: str, max_bytes: int | None = None) -> bytes:
        """Fetch a binary body, enforcing the configured size limit."""

        limit = self._max_bytes if max_bytes is None else max_bytes
        if limit < 1:
            raise ValueError("max_bytes must be positive")
        response = await self._request("GET", url, max_bytes=limit)
        if not response.ok:
            response.raise_for_status()
        return response.content

    async def _request(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        data: Mapping[str, Any] | None = None,
        content: bytes | None = None,
        json_body: Any = None,
        headers: Mapping[str, str] | None = None,
        timeout: float | httpx.Timeout | None = None,
        max_bytes: int | None = None,
    ) -> HttpResponse:
        if self._offline:
            raise OfflineModeError(
                "offline mode blocks external HTTP requests",
                context={"url": sanitize_url(url)},
            )
        self._guard.validate(url)
        request_headers = {"User-Agent": self._user_agent, "Accept": "application/json"}
        if headers:
            request_headers.update({str(key): str(value) for key, value in headers.items()})
        request_timeout = self._timeout if timeout is None else timeout
        limit = self._max_bytes if max_bytes is None else max_bytes
        started = time.monotonic()
        redirects: list[str] = []
        current_url = url
        current_params = dict(params or {})
        current_method = method
        current_data = data
        current_content = content
        current_json = json_body

        attempt = 0
        while True:
            await self._limiter.acquire(current_url)
            try:
                response = await self._client.request(
                    current_method,
                    current_url,
                    params=current_params or None,
                    data=current_data,
                    content=current_content,
                    json=current_json,
                    headers=request_headers,
                    timeout=request_timeout,
                )
                # Provider sessions must not leak cookies into later requests
                # or into any cache key.
                self._client.cookies.clear()
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                if attempt < self._max_retries:
                    await self._wait_before_retry(attempt, None)
                    attempt += 1
                    continue
                raise ProviderUnavailable(
                    "provider request failed",
                    retryable=True,
                    user_action="Check network access and retry later.",
                    context={
                        "url": sanitize_url(current_url),
                        "error": redact_text(str(exc)),
                    },
                ) from exc

            if response.status_code in _REDIRECT_STATUSES:
                location = response.headers.get("location")
                await response.aclose()
                if not location:
                    return self._response(response, current_url, started, redirects)
                if len(redirects) >= self._max_redirects:
                    raise ProviderUnavailable(
                        "too many redirects",
                        retryable=False,
                        user_action="Use a provider URL that does not redirect excessively.",
                        context={
                            "url": sanitize_url(url),
                            "max_redirects": self._max_redirects,
                        },
                    )
                next_url = urljoin(current_url, location)
                try:
                    self._guard.validate(next_url)
                except UnsafeUrl:
                    raise
                redirects.append(sanitize_url(next_url) or next_url)
                current_url = next_url
                current_params = {}
                if response.status_code == 303 or (
                    response.status_code in {301, 302} and current_method == "POST"
                ):
                    current_method = "GET"
                    current_data = None
                    current_content = None
                    current_json = None
                continue

            if response.status_code == 429 or response.status_code in _RETRYABLE_STATUSES:
                retry_after = parse_retry_after(response.headers.get("retry-after"))
                if attempt < self._max_retries:
                    await response.aclose()
                    await self._wait_before_retry(attempt, retry_after)
                    attempt += 1
                    continue
                status = response.status_code
                await response.aclose()
                if status == 429:
                    raise RateLimited(
                        "provider rate-limited the request after retries",
                        retryable=True,
                        context={"url": sanitize_url(current_url), "status": status},
                    )
                raise ProviderUnavailable(
                    f"provider returned HTTP {status} after retries",
                    retryable=True,
                    context={"url": sanitize_url(current_url), "status": status},
                )

            content_length = response.headers.get("content-length")
            if content_length is not None:
                try:
                    announced = int(content_length)
                except ValueError:
                    announced = None
                if announced is not None and announced > limit:
                    await response.aclose()
                    raise ProviderUnavailable(
                        "provider response exceeds the size limit",
                        retryable=False,
                        context={
                            "url": sanitize_url(current_url),
                            "max_bytes": limit,
                            "content_length": announced,
                        },
                    )
            if len(response.content) > limit:
                await response.aclose()
                raise ProviderUnavailable(
                    "provider response exceeds the size limit",
                    retryable=False,
                    context={"url": sanitize_url(current_url), "max_bytes": limit},
                )
            result = self._response(response, current_url, started, redirects)
            await response.aclose()
            return result

    async def _wait_before_retry(self, attempt: int, retry_after: float | None) -> None:
        if retry_after is not None:
            delay = max(0.0, retry_after)
        else:
            base = self._retry_backoff * (2**attempt)
            delay = base + (self._rng() * self._retry_jitter if self._retry_jitter else 0.0)
        if delay <= 0:
            return
        if self._sleep is not None:
            await self._sleep(delay)
        else:
            await asyncio.sleep(delay)

    @staticmethod
    def _response(
        response: httpx.Response,
        url: str,
        started: float,
        redirects: list[str],
    ) -> HttpResponse:
        return HttpResponse(
            status_code=response.status_code,
            url=sanitize_url(url) or url,
            headers=dict(response.headers),
            content=response.content,
            elapsed_ms=int((time.monotonic() - started) * 1000),
            redirects=tuple(redirects),
        )


def response_json(response: HttpResponse) -> Any:
    """Compatibility helper for callers that prefer a free function."""

    return response.json()


__all__ = [
    "HttpClient",
    "HttpResponse",
    "build_user_agent",
    "response_json",
]
