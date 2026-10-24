"""Shared helpers for HTTP-backed adapter tests (no real sleeps or DNS)."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from trackforge.adapters.http.client import HttpClient
from trackforge.adapters.http.rate_limit import HostRateLimiter
from trackforge.adapters.http.url_safety import UrlSafetyGuard

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "http"


class FakeClock:
    """A monotonic clock advanced only by the injected sleeper."""

    def __init__(self) -> None:
        self.now = 1_000.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, delay: float) -> None:
        self.sleeps.append(delay)
        self.now += delay


def fixture(name: str) -> dict[str, Any]:
    data = json.loads((FIXTURE_DIR / f"{name}.json").read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


@pytest.fixture
def fake_clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def public_guard() -> UrlSafetyGuard:
    return UrlSafetyGuard(resolver=lambda host, port: ("93.184.216.34",))


@pytest.fixture
def fake_http_client(
    fake_clock: FakeClock,
    public_guard: UrlSafetyGuard,
) -> Callable[..., HttpClient]:
    """Build an HttpClient with fake pacing, DNS, jitter, and sleeps."""

    def _build(**kwargs: Any) -> HttpClient:
        limiter = HostRateLimiter(clock=fake_clock, sleep=fake_clock.sleep)
        return HttpClient(
            contact="trackforge-tests@example.com",
            limiter=limiter,
            guard=public_guard,
            sleep=fake_clock.sleep,
            rng=lambda: 0.0,
            **kwargs,
        )

    return _build


__all__ = ["FIXTURE_DIR", "FakeClock", "fixture", "public_guard"]
