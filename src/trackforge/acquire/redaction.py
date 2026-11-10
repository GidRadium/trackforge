"""Redaction helpers for URLs and log records.

Cookies, tokens and proxy credentials must never reach logs, ``repr`` or
machine-readable output.  This module is deliberately dependency-free so it
can be used from the configuration, loader and doctor layers.
"""

# pyright: standard

from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Mapping, MutableMapping
from typing import Any
from urllib.parse import urlsplit

_USERINFO_RE = re.compile(
    r"(?P<scheme>[A-Za-z][A-Za-z0-9+.-]*://)(?P<userinfo>[^/@\s]+)@"
)
_COOKIE_HEADER_RE = re.compile(r"(?i)(cookie\s*:\s*).*")
_AUTH_HEADER_RE = re.compile(r"(?i)(authorization\s*:\s*).*")
_QUERY_SECRET_RE = re.compile(
    r"(?i)([?&](?:token|access_token|api_key|password|secret|sig|signature|"
    r"expires|exp|policy|key-pair-id|x-amz-[^=&\s]+)=)[^&\s]+"
)
_SENTINELS = frozenset({"none", "env"})


def redact_proxy(proxy: str | None) -> str:
    """Return a display-safe form of a proxy setting.

    ``none``/``env`` are configuration sentinels rather than URLs and are
    shown as-is.  Credentials are always removed.
    """
    if proxy is None or not proxy.strip():
        return "<none>"
    value = proxy.strip()
    if value.lower() in _SENTINELS:
        return value.lower()
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        port = parsed.port
    except ValueError:
        return "<invalid>"
    if not parsed.scheme or not host:
        return "<invalid>"
    host_display = f"[{host}]" if ":" in host and not host.startswith("[") else host
    if port is not None:
        host_display = f"{host_display}:{port}"
    return f"{parsed.scheme.lower()}://{host_display}"


def redact_text(text: str, secrets: Iterable[str] = ()) -> str:
    """Remove proxy credentials, cookie headers and known secret values."""
    rendered = str(text)
    for secret in sorted((str(item) for item in secrets if item), key=len, reverse=True):
        rendered = rendered.replace(secret, "<redacted>")
    rendered = _USERINFO_RE.sub(r"\g<scheme><redacted>@", rendered)
    rendered = _COOKIE_HEADER_RE.sub(r"\1<redacted>", rendered)
    rendered = _AUTH_HEADER_RE.sub(r"\1<redacted>", rendered)
    rendered = _QUERY_SECRET_RE.sub(r"\1<redacted>", rendered)
    return rendered


class RedactingLoggerAdapter(logging.LoggerAdapter):
    """A ``logging.LoggerAdapter`` that redacts secrets from every message."""

    def __init__(
        self,
        logger: logging.Logger,
        secrets: Iterable[str] = (),
        extra: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(logger, dict(extra or {}))
        self._secrets = tuple(str(secret) for secret in secrets if secret)

    def process(
        self,
        msg: object,
        kwargs: MutableMapping[str, Any],
    ) -> tuple[str, MutableMapping[str, Any]]:
        return redact_text(str(msg), self._secrets), kwargs
