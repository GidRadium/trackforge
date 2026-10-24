"""Explicit per-site proxy routing for network capabilities.

TrackForge deliberately does not inherit ``HTTP(S)_PROXY``/``ALL_PROXY`` from
the process environment.  Network components receive a proxy only when it is
configured for that component, and ``yt-dlp`` additionally supports per-site
routes because a proxy that is required for YouTube may be forbidden for
Yandex Music.

An empty proxy string means *direct connection*.  A matching route therefore
overrides the default even when its value is empty; this is what makes an
explicit ``music.yandex.ru = ""`` rule possible.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import urlsplit

from trackforge.domain.errors import DomainValidationError


@dataclass(frozen=True, slots=True)
class ProxyRoute:
    """One host-suffix route; an empty proxy means explicitly direct."""

    host_suffix: str
    proxy: str | None = None

    def __post_init__(self) -> None:
        suffix = _normalize_host_suffix(self.host_suffix)
        object.__setattr__(self, "host_suffix", suffix)
        object.__setattr__(self, "proxy", _optional_proxy(self.proxy))

    def matches(self, host: str) -> bool:
        candidate = host.strip().casefold().rstrip(".")
        return candidate == self.host_suffix or candidate.endswith(f".{self.host_suffix}")


@dataclass(frozen=True, slots=True)
class ProxyRouter:
    """Default proxy plus the most-specific matching host route."""

    default: str | None = None
    routes: tuple[ProxyRoute, ...] = ()

    def __post_init__(self) -> None:
        routes = tuple(self.routes)
        for route in routes:
            if not isinstance(route, ProxyRoute):
                raise DomainValidationError("routes must be ProxyRoute values")
        # Longest suffix wins: music.youtube.com before youtube.com.
        routes = tuple(
            sorted(
                routes,
                key=lambda item: (len(item.host_suffix.split(".")), len(item.host_suffix)),
                reverse=True,
            )
        )
        object.__setattr__(self, "default", _optional_proxy(self.default))
        object.__setattr__(self, "routes", routes)

    @classmethod
    def from_mapping(
        cls,
        default: str | None = None,
        routes: Mapping[str, str] | None = None,
    ) -> ProxyRouter:
        return cls(
            default=default,
            routes=tuple(ProxyRoute(host, proxy) for host, proxy in (routes or {}).items()),
        )

    def route_for(self, url: str) -> ProxyRoute | None:
        """Return the first most-specific matching route, if any."""

        host = _host_from_url(url)
        if host is None:
            return None
        return next((route for route in self.routes if route.matches(host)), None)

    def proxy_for(self, url: str) -> str | None:
        """Resolve the proxy for ``url``; ``None`` means direct connection."""

        route = self.route_for(url)
        if route is not None:
            return route.proxy
        return self.default

    @property
    def configured(self) -> bool:
        return self.default is not None or any(route.proxy is not None for route in self.routes)


def _optional_proxy(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise DomainValidationError("proxy must be a string or None")
    text = value.strip()
    return text or None


def _normalize_host_suffix(value: str) -> str:
    if not isinstance(value, str):
        raise DomainValidationError("proxy route host must be a string")
    text = value.strip().casefold().rstrip(".")
    if text.startswith("*."):
        text = text[2:]
    if (
        not text
        or "/" in text
        or ":" in text
        or "*" in text
        or any(char.isspace() for char in text)
    ):
        raise DomainValidationError(
            "proxy route host must be a bare hostname or suffix",
            context={"host": value},
        )
    return text


def _host_from_url(url: str) -> str | None:
    try:
        parsed = urlsplit(url)
    except ValueError:
        return None
    host = (parsed.hostname or "").strip().casefold().rstrip(".")
    return host or None


__all__ = ["ProxyRoute", "ProxyRouter"]
