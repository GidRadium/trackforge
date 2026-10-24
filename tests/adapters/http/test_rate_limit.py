"""Per-host rate limiter tests with a fake clock (no real sleeps)."""

from __future__ import annotations

import pytest
from tests.adapters.conftest import FakeClock

from trackforge.adapters.http.rate_limit import (
    DEFAULT_HOST_RATES,
    HostRateLimiter,
    host_rate,
    normalize_host,
    parse_retry_after,
)


@pytest.mark.asyncio
async def test_musicbrainz_allows_one_request_per_second(fake_clock: FakeClock) -> None:
    limiter = HostRateLimiter(clock=fake_clock, sleep=fake_clock.sleep)
    start = fake_clock.now

    await limiter.acquire("https://musicbrainz.org/ws/2/recording")
    await limiter.acquire("https://musicbrainz.org/ws/2/recording")

    assert fake_clock.now - start == pytest.approx(1.0)
    assert fake_clock.sleeps == [pytest.approx(1.0)]


@pytest.mark.asyncio
async def test_acoustid_allows_three_requests_per_second(fake_clock: FakeClock) -> None:
    limiter = HostRateLimiter(clock=fake_clock, sleep=fake_clock.sleep)

    await limiter.acquire("api.acoustid.org")
    after_first = fake_clock.now
    await limiter.acquire("api.acoustid.org")

    assert fake_clock.now - after_first == pytest.approx(1.0 / 3.0)


@pytest.mark.asyncio
async def test_unknown_hosts_use_conservative_rate(fake_clock: FakeClock) -> None:
    limiter = HostRateLimiter(clock=fake_clock, sleep=fake_clock.sleep)

    assert limiter.rate_for("example.com") == pytest.approx(2.0)
    await limiter.acquire("example.com")
    start = fake_clock.now
    await limiter.acquire("example.com")

    assert fake_clock.now - start == pytest.approx(0.5)


@pytest.mark.asyncio
async def test_global_limit_covers_distinct_hosts(fake_clock: FakeClock) -> None:
    limiter = HostRateLimiter(global_rate=1.0, clock=fake_clock, sleep=fake_clock.sleep)

    await limiter.acquire("one.example.com")
    start = fake_clock.now
    await limiter.acquire("two.example.com")

    assert fake_clock.now - start == pytest.approx(1.0)


def test_known_host_rates_and_host_normalization() -> None:
    assert DEFAULT_HOST_RATES["musicbrainz.org"] == 1.0
    assert DEFAULT_HOST_RATES["acoustid.org"] == 3.0
    assert host_rate("https://musicbrainz.org/ws/2") == 1.0
    assert host_rate("https://api.acoustid.org/v2/lookup") == 3.0
    assert normalize_host("https://MusicBrainz.org:443/x") == "musicbrainz.org"
    assert normalize_host("https://lrclib.net/api/get") == "lrclib.net"


def test_sleep_is_injected_not_real() -> None:
    """The default sleeper exists, but the test suite only uses the fake."""

    limiter = HostRateLimiter()
    assert limiter.sleep_calls == 0


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("0", 0.0),
        ("2", 2.0),
        ("2.5", 2.5),
        ("garbage", None),
        ("", None),
        (None, None),
    ],
)
def test_parse_retry_after_seconds(value: str | None, expected: float | None) -> None:
    assert parse_retry_after(value) == expected


def test_parse_retry_after_http_date() -> None:
    # Thu, 01 Jan 1970 00:00:02 GMT is two seconds after the epoch.
    assert parse_retry_after("Thu, 01 Jan 1970 00:00:02 GMT", now=0.0) == pytest.approx(2.0)
