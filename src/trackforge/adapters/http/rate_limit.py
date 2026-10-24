"""Per-host and global request pacing for external metadata providers.

The limiter is intentionally small and transport-independent.  It uses a
token bucket per host and a second bucket for the aggregate request rate.
Time and sleep are injectable so unit tests can use a fake clock and never
sleep for real.

Rate limits follow the public expectations documented in ``05-sources.md``:

* MusicBrainz: 1 request/second per IP.
* AcoustID: at most 3 requests/second.
* Cover Art Archive, LRCLIB, iTunes and Deezer: a conservative 2 requests/s.
* Unknown hosts: 2 requests/s.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from urllib.parse import urlsplit

Clock = Callable[[], float]
Sleeper = Callable[[float], Awaitable[None]]

MUSICBRAINZ_RATE = 1.0
ACOUSTID_RATE = 3.0
CONSERVATIVE_RATE = 2.0
GLOBAL_RATE = 10.0

DEFAULT_HOST_RATES: dict[str, float] = {
    "musicbrainz.org": MUSICBRAINZ_RATE,
    "acoustid.org": ACOUSTID_RATE,
    "coverartarchive.org": CONSERVATIVE_RATE,
    "lrclib.net": CONSERVATIVE_RATE,
    "itunes.apple.com": CONSERVATIVE_RATE,
    "api.deezer.com": CONSERVATIVE_RATE,
}

_GLOBAL_KEY = "\x00global"


@dataclass(slots=True)
class _Bucket:
    rate: float
    tokens: float
    updated_at: float

    @classmethod
    def started(cls, rate: float, now: float) -> _Bucket:
        return cls(rate=rate, tokens=1.0, updated_at=now)

    def refill(self, now: float) -> None:
        elapsed = max(0.0, now - self.updated_at)
        if elapsed:
            self.tokens = min(1.0, self.tokens + elapsed * self.rate)
            self.updated_at = now


def normalize_host(value: str) -> str:
    """Return a stable host key for a URL or bare hostname."""

    text = str(value).strip()
    if not text:
        return "unknown"
    if "://" not in text:
        text = f"//{text}"
    try:
        parsed = urlsplit(text)
    except ValueError:
        return "unknown"
    host = (parsed.hostname or parsed.netloc or "unknown").strip().rstrip(".").casefold()
    port = parsed.port
    if port is not None and port not in {80, 443}:
        host = f"{host}:{port}"
    return host or "unknown"


def host_rate(host: str, policies: Mapping[str, float] | None = None) -> float:
    """Look up the configured rate for a host (requests per second)."""

    rules = DEFAULT_HOST_RATES if policies is None else policies
    key = normalize_host(host)
    bare = key.split(":", 1)[0]
    if bare in rules:
        return float(rules[bare])
    for domain, rate in rules.items():
        domain_key = normalize_host(domain)
        if bare == domain_key or bare.endswith(f".{domain_key}"):
            return float(rate)
    return CONSERVATIVE_RATE


class HostRateLimiter:
    """Async token-bucket limiter with per-host and global pacing."""

    def __init__(
        self,
        policies: Mapping[str, float] | None = None,
        *,
        rates: Mapping[str, float] | None = None,
        rules: Mapping[str, float] | None = None,
        default_rate: float = CONSERVATIVE_RATE,
        global_rate: float = GLOBAL_RATE,
        clock: Clock | None = None,
        sleep: Sleeper | None = None,
        time_fn: Clock | None = None,
    ) -> None:
        if default_rate <= 0 or global_rate <= 0:
            raise ValueError("rate limits must be positive")
        selected = policies if policies is not None else rates if rates is not None else rules
        self._policies = dict(DEFAULT_HOST_RATES if selected is None else selected)
        self._default_rate = float(default_rate)
        self._global_rate = float(global_rate)
        self._clock: Clock = clock or time_fn or time.monotonic
        self._sleep: Sleeper | None = sleep
        self._buckets: dict[str, _Bucket] = {}
        self._lock = asyncio.Lock()
        self._sleep_calls = 0

    @property
    def policies(self) -> Mapping[str, float]:
        return dict(self._policies)

    @property
    def sleep_calls(self) -> int:
        """Number of sleeper invocations; useful for deterministic tests."""

        return self._sleep_calls

    def rate_for(self, host: str) -> float:
        """Return the effective requests/second configured for ``host``."""

        key = normalize_host(host)
        bare = key.split(":", 1)[0]
        if bare in self._policies:
            return float(self._policies[bare])
        for domain, rate in self._policies.items():
            domain_key = normalize_host(domain)
            if bare == domain_key or bare.endswith(f".{domain_key}"):
                return float(rate)
        return self._default_rate

    async def acquire(self, host: str, *, tokens: float = 1.0) -> float:
        """Wait until one request is allowed and return the total waited time."""

        if tokens <= 0:
            raise ValueError("tokens must be positive")
        key = normalize_host(host)
        waited = 0.0
        waited += await self._acquire_bucket(key, self.rate_for(key), tokens)
        waited += await self._acquire_bucket(_GLOBAL_KEY, self._global_rate, tokens)
        return waited

    async def wait(self, host: str) -> float:
        """Alias for :meth:`acquire` used by callers that think in waits."""

        return await self.acquire(host)

    async def limit(self, host: str) -> float:
        """Alias for :meth:`acquire`."""

        return await self.acquire(host)

    async def _acquire_bucket(self, key: str, rate: float, tokens: float) -> float:
        waited = 0.0
        while True:
            async with self._lock:
                now = self._clock()
                bucket = self._buckets.get(key)
                if bucket is None:
                    bucket = _Bucket.started(rate, now)
                    self._buckets[key] = bucket
                bucket.rate = rate
                bucket.refill(now)
                if bucket.tokens >= tokens:
                    bucket.tokens -= tokens
                    return waited
                deficit = tokens - bucket.tokens
                delay = deficit / bucket.rate
            awaited = max(0.0, delay)
            self._sleep_calls += 1
            await self._sleep_for(awaited)
            waited += awaited

    async def _sleep_for(self, delay: float) -> None:
        if delay <= 0:
            return
        if self._sleep is not None:
            await self._sleep(delay)
        else:
            await asyncio.sleep(delay)


# Compatibility aliases used by callers and tests that prefer shorter names.
RateLimiter = HostRateLimiter
PerHostRateLimiter = HostRateLimiter


def parse_retry_after(value: str | None, *, now: float | None = None) -> float | None:
    """Parse an HTTP ``Retry-After`` header into seconds.

    Both delta-seconds and HTTP-date forms are accepted.  Invalid values return
    ``None`` so the caller can fall back to exponential backoff.
    """

    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    try:
        seconds = float(text)
    except ValueError:
        pass
    else:
        return max(0.0, seconds)
    try:
        from email.utils import parsedate_to_datetime

        parsed = parsedate_to_datetime(text)
        reference = time.time() if now is None else now
        return max(0.0, parsed.timestamp() - reference)
    except (TypeError, ValueError, OverflowError):
        return None


def rate_limit_for(host: str, policies: Mapping[str, float] | None = None) -> float:
    """Alias for :func:`host_rate`."""

    return host_rate(host, policies)


DEFAULT_RATES = DEFAULT_HOST_RATES
RATE_LIMITS = DEFAULT_HOST_RATES


__all__ = [
    "ACOUSTID_RATE",
    "CONSERVATIVE_RATE",
    "DEFAULT_HOST_RATES",
    "DEFAULT_RATES",
    "GLOBAL_RATE",
    "MUSICBRAINZ_RATE",
    "RATE_LIMITS",
    "HostRateLimiter",
    "PerHostRateLimiter",
    "RateLimiter",
    "host_rate",
    "normalize_host",
    "parse_retry_after",
    "rate_limit_for",
]
