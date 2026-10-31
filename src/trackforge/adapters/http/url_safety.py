"""SSRF-focused URL validation.

yt-dlp accepts arbitrary user URLs.  Before spawning it we parse the URL,
reject non-HTTP schemes and obvious local names, resolve DNS, and reject any
address that is not globally routable.  Redirects are not followed here; the
:class:`UrlSafetyGuard` exposes a bounded chain validator that a caller can use
when it owns the redirect loop.

The guard is deliberately small and dependency-free.  DNS resolution is
best-effort: a host that does not resolve cannot be connected to by this
process, so it is allowed to continue to the downloader (which will report its
own typed error).  A caller that needs strict fail-closed behaviour can pass
``strict_dns=True``.
"""

from __future__ import annotations

import ipaddress
import re
import socket
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import SplitResult, parse_qsl, urlencode, urlsplit, urlunsplit

from trackforge.domain.errors import UnsafeUrl

_MAX_URL_LENGTH = 8_192
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_LOCAL_SUFFIXES = (".localhost", ".local", ".internal", ".home.arpa")
_LOCAL_NAMES = frozenset({"localhost", "localhost.localdomain", "ip6-localhost"})
_ALLOWED_SCHEMES = frozenset({"http", "https"})
_DEFAULT_PORTS = {"http": 80, "https": 443}
_YOUTUBE_HOSTS = frozenset(
    {
        "youtube.com",
        "www.youtube.com",
        "m.youtube.com",
        "music.youtube.com",
        "youtube-nocookie.com",
        "www.youtube-nocookie.com",
        "youtu.be",
    }
)
_YOUTUBE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,150}$")

Resolver = Callable[[str, int], Sequence[str]]


@dataclass(frozen=True, slots=True)
class SafeUrl:
    """A parsed URL that passed the scheme and address policy."""

    url: str
    scheme: str
    host: str
    port: int
    addresses: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.url or not self.host:
            raise UnsafeUrl("URL must include a host")


def parse_http_url(value: str) -> SafeUrl:
    """Parse one http(s) URL without performing DNS resolution."""

    if not isinstance(value, str) or not value.strip():
        raise UnsafeUrl("URL must not be empty")
    text = value.strip()
    if len(text) > _MAX_URL_LENGTH:
        raise UnsafeUrl(
            "URL is too long",
            context={"length": len(text), "max_length": _MAX_URL_LENGTH},
        )
    if _CONTROL_RE.search(text):
        raise UnsafeUrl("URL contains control characters")
    try:
        parts: SplitResult = urlsplit(text)
    except ValueError as exc:
        raise UnsafeUrl(
            "URL cannot be parsed",
            context={"error": str(exc)},
        ) from exc
    scheme = parts.scheme.lower()
    if scheme not in _ALLOWED_SCHEMES:
        raise UnsafeUrl(
            "only http and https URLs are supported",
            context={"scheme": parts.scheme or "<missing>"},
        )
    if not parts.hostname:
        raise UnsafeUrl("URL must include a host")
    if "\\" in parts.netloc:
        raise UnsafeUrl("URL authority must not contain backslashes")
    if parts.username is not None or parts.password is not None:
        # Userinfo is allowed but never used for authentication by the source.
        # Strip it from the URL we pass to the downloader.
        pass
    try:
        host = parts.hostname.encode("idna").decode("ascii").rstrip(".").lower()
    except UnicodeError as exc:
        raise UnsafeUrl("URL host is not a valid domain name") from exc
    if not host:
        raise UnsafeUrl("URL must include a host")
    if any(character.isspace() for character in host):
        raise UnsafeUrl("URL host must not contain whitespace")
    if host in _LOCAL_NAMES or host.endswith(_LOCAL_SUFFIXES):
        raise UnsafeUrl(
            "local hostnames are blocked",
            context={"host": host},
        )
    try:
        port = parts.port
    except ValueError as exc:
        raise UnsafeUrl("URL port is invalid") from exc
    if port is None:
        port = _DEFAULT_PORTS[scheme]
    if port < 1 or port > 65535:
        raise UnsafeUrl("URL port is outside the valid range", context={"port": port})
    return SafeUrl(url=text, scheme=scheme, host=host, port=port)


def _is_public_address(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    if (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    ):
        return False
    # ``is_global`` also rejects documentation and benchmarking ranges.
    return bool(address.is_global)


def _default_resolver(host: str, port: int) -> tuple[str, ...]:
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (socket.gaierror, OSError):
        return ()
    addresses: list[str] = []
    for info in infos:
        sockaddr: Any = info[4]
        if isinstance(sockaddr, tuple) and sockaddr:
            address = str(sockaddr[0])
            if address not in addresses:
                addresses.append(address)
    return tuple(addresses)


class UrlSafetyGuard:
    """Validate URLs against the SSRF policy before invoking a downloader."""

    def __init__(
        self,
        *,
        allow_private: bool = False,
        max_redirects: int = 5,
        strict_dns: bool = False,
        resolver: Resolver | None = None,
    ) -> None:
        if isinstance(max_redirects, bool) or not isinstance(max_redirects, int):
            raise ValueError("max_redirects must be an integer")
        if max_redirects < 0:
            raise ValueError("max_redirects must not be negative")
        self.allow_private = bool(allow_private)
        self.max_redirects = max_redirects
        self.strict_dns = bool(strict_dns)
        self._resolver: Resolver = resolver or _default_resolver

    def validate(self, value: str) -> SafeUrl:
        """Parse, resolve, and validate one URL."""

        parsed = parse_http_url(value)
        addresses: tuple[str, ...]
        try:
            literal = ipaddress.ip_address(parsed.host.split("%", 1)[0])
        except ValueError:
            literal = None
        if literal is not None:
            addresses = (str(literal),)
        else:
            try:
                addresses = tuple(self._resolver(parsed.host, parsed.port))
            except (socket.gaierror, OSError):
                addresses = ()
        if addresses and not self.allow_private:
            unsafe = [address for address in addresses if not _is_public_address(address)]
            if unsafe:
                raise UnsafeUrl(
                    "URL resolves to a private, local, or reserved address",
                    context={
                        "host": parsed.host,
                        "addresses": unsafe,
                    },
                )
        if not addresses and self.strict_dns:
            raise UnsafeUrl(
                "URL host could not be resolved",
                context={"host": parsed.host},
            )
        return SafeUrl(
            url=parsed.url,
            scheme=parsed.scheme,
            host=parsed.host,
            port=parsed.port,
            addresses=addresses,
        )

    def supports(self, value: str) -> bool:
        """Return whether ``value`` is a syntactically safe http(s) URL.

        This is intentionally network-free so source discovery stays cheap.
        """

        try:
            parsed = parse_http_url(value)
        except UnsafeUrl:
            return False
        try:
            literal = ipaddress.ip_address(parsed.host.split("%", 1)[0])
        except ValueError:
            return True
        return self.allow_private or _is_public_address(str(literal))

    def validate_redirect_chain(self, urls: Sequence[str]) -> tuple[SafeUrl, ...]:
        """Validate a redirect chain with a hard hop limit."""

        chain = tuple(urls)
        if not chain:
            raise UnsafeUrl("redirect chain must not be empty")
        if len(chain) > self.max_redirects + 1:
            raise UnsafeUrl(
                "too many redirects",
                context={"hops": len(chain) - 1, "max_redirects": self.max_redirects},
            )
        return tuple(self.validate(url) for url in chain)

    def check_redirect_chain(self, urls: Sequence[str]) -> tuple[SafeUrl, ...]:
        """Alias for :meth:`validate_redirect_chain`."""

        return self.validate_redirect_chain(urls)


def validate_redirect_chain(
    urls: Sequence[str],
    *,
    max_redirects: int = 5,
) -> tuple[SafeUrl, ...]:
    """Validate a redirect chain with a default guard."""

    return UrlSafetyGuard(max_redirects=max_redirects).validate_redirect_chain(urls)


def validate_url(value: str, *, allow_private: bool = False, strict_dns: bool = False) -> SafeUrl:
    """Validate one URL with a default guard."""

    return UrlSafetyGuard(
        allow_private=allow_private,
        strict_dns=strict_dns,
    ).validate(value)


def is_safe_url(value: str) -> bool:
    """Return whether a URL passes the network-free policy."""

    return UrlSafetyGuard().supports(value)


def ensure_safe_url(
    value: str,
    *,
    allow_private: bool = False,
    strict_dns: bool = False,
) -> str:
    """Validate a URL and return it unchanged for convenient call sites."""

    validate_url(value, allow_private=allow_private, strict_dns=strict_dns)
    return value


def is_private_address(value: str) -> bool:
    """Return whether an IP literal is not globally routable."""

    return not _is_public_address(value)


# Common names used by security-focused call sites.
SSRFGuard = UrlSafetyGuard
UrlGuard = UrlSafetyGuard


def sanitize_url(value: str | None) -> str | None:
    """Remove userinfo, secrets, and fragments from a URL for display/logging.

    Public YouTube identifiers (``v`` for a video, ``list`` for a playlist)
    are preserved because they are the identity of the source, not a secret.
    Every other query parameter is dropped.
    """

    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        parts = urlsplit(text)
    except ValueError:
        return text.split("?", 1)[0].split("#", 1)[0]
    if not parts.scheme or not parts.netloc:
        return text.split("?", 1)[0].split("#", 1)[0]
    host = (parts.hostname or "").casefold()
    try:
        port = parts.port
    except ValueError:
        port = None
    display_host = f"[{host}]" if ":" in host else host
    if port is not None and port != _DEFAULT_PORTS.get(parts.scheme.lower()):
        display_host = f"{display_host}:{port}"
    query = _safe_source_query(parts.path, host, parts.query)
    return urlunsplit((parts.scheme, display_host, parts.path, query, ""))


def _safe_source_query(path: str, host: str, query: str) -> str:
    if not query or host not in _YOUTUBE_HOSTS:
        return ""
    allowed = (
        {"v"}
        if path.rstrip("/") == "/watch"
        else {"list"}
        if path.rstrip("/") == "/playlist"
        else set()
    )
    if not allowed:
        return ""
    kept: list[tuple[str, str]] = []
    for key, item in parse_qsl(query, keep_blank_values=False):
        if key not in allowed or not _YOUTUBE_ID_RE.fullmatch(item):
            continue
        kept.append((key, item))
    return urlencode(kept)


def redact_url(value: str | None) -> str | None:
    """Alias used by logging/error paths."""

    return sanitize_url(value)


def redact_text(text: str) -> str:
    """Replace every URL query/fragment in ``text`` with a safe form."""

    def replace(match: re.Match[str]) -> str:
        return sanitize_url(match.group(0)) or ""

    return re.sub(r"https?://[^\s]+", replace, text)


__all__ = [
    "SSRFGuard",
    "SafeUrl",
    "UnsafeUrl",
    "UrlGuard",
    "UrlSafetyGuard",
    "ensure_safe_url",
    "is_private_address",
    "is_safe_url",
    "parse_http_url",
    "redact_text",
    "redact_url",
    "sanitize_url",
    "validate_redirect_chain",
    "validate_url",
]
