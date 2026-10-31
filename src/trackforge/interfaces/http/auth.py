"""Local bearer-token authentication for the optional HTTP API.

The token is generated per server start, written with owner-only permissions
under ``state_dir``, and compared in constant time.  The token value is never
included in startup output or error payloads.
"""

from __future__ import annotations

import hmac
import os
import re
import secrets
from pathlib import Path

from trackforge.config import TrackForgeConfig

TOKEN_BYTES = 48
TOKEN_FILENAME = "http-token"  # noqa: S105 - a filename, not a password
AUTHORIZATION_HEADER = "Authorization"
_LOCAL_ORIGIN_RE = re.compile(r"^http://(?:127\.0\.0\.1|localhost)(?::\d{1,5})?$")


def token_path(config: TrackForgeConfig) -> Path:
    """Return the configured token file path (``state_dir/http-token``)."""

    return Path(config.state_dir) / TOKEN_FILENAME


def generate_token() -> str:
    """Generate an unguessable URL-safe bearer token (48 random bytes)."""

    return secrets.token_urlsafe(TOKEN_BYTES)


def write_token(path: Path, token: str) -> Path:
    """Write ``token`` to ``path`` with mode 0600, replacing any old value."""

    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(destination, flags | nofollow, 0o600)
    try:
        os.fchmod(fd, 0o600)
        os.write(fd, token.encode("ascii"))
    finally:
        os.close(fd)
    return destination


def ensure_token(
    config: TrackForgeConfig,
    *,
    token: str | None = None,
    path: Path | None = None,
) -> tuple[str, Path]:
    """Generate (or accept) a token and persist it with owner-only mode."""

    value = token if token is not None else generate_token()
    if not value or len(value) < 32:
        raise ValueError("the HTTP token must contain at least 32 characters")
    destination = path if path is not None else token_path(config)
    write_token(destination, value)
    return value, destination


def verify_bearer(header: str | None, expected: str) -> bool:
    """Return whether ``header`` carries the expected bearer token.

    ``hmac.compare_digest`` keeps the comparison timing-safe.  A missing or
    malformed header is rejected without falling back to query parameters.
    """

    if not header or not expected:
        return False
    scheme, separator, value = header.partition(" ")
    if not separator or scheme.casefold() != "bearer":
        return False
    candidate = value.strip()
    if not candidate:
        return False
    return hmac.compare_digest(candidate.encode("utf-8"), expected.encode("utf-8"))


def is_local_origin(origin: str) -> bool:
    """Return whether ``origin`` is a loopback HTTP origin."""

    return _LOCAL_ORIGIN_RE.fullmatch(origin.strip()) is not None


def is_local_host(host: str) -> bool:
    """Return whether ``host`` is an acceptable local bind address."""

    normalized = host.strip().casefold()
    return normalized in {"127.0.0.1", "localhost", "::1"}


__all__ = [
    "AUTHORIZATION_HEADER",
    "TOKEN_BYTES",
    "TOKEN_FILENAME",
    "ensure_token",
    "generate_token",
    "is_local_host",
    "is_local_origin",
    "token_path",
    "verify_bearer",
    "write_token",
]
