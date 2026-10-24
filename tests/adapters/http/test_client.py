"""HTTP client tests: retries, redirects, SSRF, redaction, and size limits."""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest
import respx
from tests.adapters.conftest import FakeClock

from trackforge.adapters.http.client import HttpClient
from trackforge.adapters.http.url_safety import UrlSafetyGuard
from trackforge.domain.errors import OfflineModeError, ProviderUnavailable, UnsafeUrl

pytestmark = pytest.mark.asyncio


async def test_user_agent_has_trackforge_and_contact(
    fake_http_client: Callable[..., HttpClient],
) -> None:
    client = fake_http_client()
    assert client.user_agent.startswith("TrackForge/")
    assert "trackforge-tests@example.com" in client.user_agent
    await client.aclose()


async def test_retries_5xx_and_honours_backoff(
    fake_http_client: Callable[..., HttpClient],
    fake_clock: FakeClock,
) -> None:
    client = fake_http_client(max_retries=2, retry_backoff=1.0, retry_jitter=0.0)
    with respx.mock:
        route = respx.get("https://api.example.com/data").mock(
            side_effect=[
                httpx.Response(503),
                httpx.Response(500),
                httpx.Response(200, json={"ok": True}),
            ]
        )
        response = await client.get("https://api.example.com/data")

    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert route.call_count == 3
    assert fake_clock.sleeps == [pytest.approx(1.0), pytest.approx(2.0)]
    await client.aclose()


async def test_retry_after_header_is_respected(
    fake_http_client: Callable[..., HttpClient],
    fake_clock: FakeClock,
) -> None:
    client = fake_http_client(max_retries=1, retry_backoff=10.0)
    with respx.mock:
        respx.get("https://api.example.com/data").mock(
            side_effect=[
                httpx.Response(429, headers={"Retry-After": "0"}),
                httpx.Response(200, json={"ok": True}),
            ]
        )
        await client.get("https://api.example.com/data")

    # Retry-After: 0 avoids the retry backoff, while the host limiter still
    # keeps the two requests at least 0.5s apart.
    assert fake_clock.sleeps == [pytest.approx(0.5)]
    await client.aclose()


async def test_exhausted_retries_raise_provider_unavailable(
    fake_http_client: Callable[..., HttpClient],
) -> None:
    client = fake_http_client(max_retries=1, retry_backoff=0.0)
    with respx.mock:
        respx.get("https://api.example.com/data").mock(return_value=httpx.Response(503))
        with pytest.raises(ProviderUnavailable) as caught:
            await client.get("https://api.example.com/data", params={"api_key": "secret"})

    assert caught.value.retryable is True
    assert "secret" not in str(caught.value.context)
    await client.aclose()


async def test_redirects_are_followed_and_bounded(
    fake_http_client: Callable[..., HttpClient],
) -> None:
    client = fake_http_client(max_redirects=1, max_retries=0)
    with respx.mock:
        respx.get("https://api.example.com/one").mock(
            return_value=httpx.Response(302, headers={"Location": "/two"})
        )
        respx.get("https://api.example.com/two").mock(
            return_value=httpx.Response(302, headers={"Location": "/three"})
        )
        with pytest.raises(ProviderUnavailable, match="too many redirects"):
            await client.get("https://api.example.com/one")
    await client.aclose()


async def test_redirect_to_private_address_is_blocked(
    fake_http_client: Callable[..., HttpClient],
) -> None:
    client = fake_http_client(max_retries=0)
    with respx.mock:
        respx.get("https://api.example.com/one").mock(
            return_value=httpx.Response(302, headers={"Location": "http://127.0.0.1/private"})
        )
        with pytest.raises(UnsafeUrl):
            await client.get("https://api.example.com/one")
    await client.aclose()


async def test_offline_fails_before_any_request(
    fake_http_client: Callable[..., HttpClient],
) -> None:
    client = fake_http_client(offline=True)
    with respx.mock:
        with pytest.raises(OfflineModeError):
            await client.get("https://api.example.com/data")
    client._guard = UrlSafetyGuard(resolver=lambda host, port: ("93.184.216.34",))
    await client.aclose()


async def test_get_bytes_enforces_max_size(
    fake_http_client: Callable[..., HttpClient],
) -> None:
    client = fake_http_client(max_bytes=1024, max_retries=0)
    with respx.mock:
        respx.get("https://api.example.com/image").mock(
            return_value=httpx.Response(
                200,
                content=b"x" * 32,
                headers={"Content-Type": "image/png"},
            )
        )
        assert await client.get_bytes("https://api.example.com/image") == b"x" * 32
        with pytest.raises(ProviderUnavailable, match="size limit"):
            await client.get_bytes("https://api.example.com/image", max_bytes=16)
    await client.aclose()


async def test_request_headers_include_user_agent(
    fake_http_client: Callable[..., HttpClient],
) -> None:
    client = fake_http_client()
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(dict(request.headers))
        return httpx.Response(200, json={})

    transport = httpx.MockTransport(handler)
    injected = HttpClient(
        user_agent="TrackForge/test (contact@example.com)",
        client=httpx.AsyncClient(transport=transport),
        guard=UrlSafetyGuard(resolver=lambda host, port: ("93.184.216.34",)),
    )
    await injected.get("https://api.example.com/data")
    assert captured["user-agent"] == "TrackForge/test (contact@example.com)"
    await injected.aclose()
    await client.aclose()


async def test_cookies_are_not_persisted_between_requests() -> None:
    seen: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("cookie"))
        return httpx.Response(
            200,
            json={},
            headers={"Set-Cookie": "session=secret; Path=/"},
        )

    transport = httpx.MockTransport(handler)
    client = HttpClient(
        client=httpx.AsyncClient(transport=transport),
        guard=UrlSafetyGuard(resolver=lambda host, port: ("93.184.216.34",)),
    )
    await client.get("https://api.example.com/one")
    await client.get("https://api.example.com/two")
    assert seen == [None, None]
    await client.aclose()


async def test_http_client_proxy_is_explicit_and_opt_in() -> None:
    from trackforge.adapters.http.client import HttpClient

    assert HttpClient().proxy_configured is False
    assert HttpClient(proxy="http://proxy.example:3128").proxy_configured is True
    assert HttpClient(proxy="   ").proxy_configured is False
