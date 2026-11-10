"""Per-source authentication primitives: cookies and proxy.

The module deliberately performs no network I/O.  It validates the configured
cookie file and proxy setting and gives loaders a single preflight guard to
call before any request.  Cookie files are used in place; yt-dlp may refresh
them, so TrackForge does not make a private copy.
"""

# pyright: standard

from __future__ import annotations

import os
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from .config import SourceProfile
from .errors import AuthRequired, ConfigError, CookieError
from .redaction import redact_proxy

_PROXY_SCHEMES = frozenset(
    {"http", "https", "socks4", "socks4a", "socks5", "socks5h"}
)
_PROXY_ENV_KEYS = (
    "HTTPS_PROXY",
    "https_proxy",
    "HTTP_PROXY",
    "http_proxy",
    "ALL_PROXY",
    "all_proxy",
)


@dataclass(frozen=True, slots=True)
class CookieFileCheck:
    """Offline inspection result for a Netscape cookie file."""

    path: Path
    exists: bool = False
    is_file: bool = False
    readable: bool = False
    mode: int | None = None
    world_readable: bool = False
    has_header: bool = False
    format_ok: bool = False
    data_lines: int = 0
    error: str | None = None

    @property
    def private(self) -> bool:
        return self.mode is not None and not self.world_readable

    @property
    def ok(self) -> bool:
        return (
            self.exists
            and self.is_file
            and self.readable
            and self.format_ok
            and self.error is None
        )


def inspect_cookie_file(path: str | Path) -> CookieFileCheck:
    """Check existence, permissions and the Netscape field layout.

    Cookie values are never returned, logged or included in errors.  Only the
    structural result is exposed.
    """
    cookie_path = Path(path).expanduser()
    try:
        file_stat = cookie_path.stat()
    except FileNotFoundError:
        return CookieFileCheck(path=cookie_path, error="file does not exist")
    except OSError as exc:
        return CookieFileCheck(
            path=cookie_path,
            error=f"cannot stat file: {exc.strerror or 'OS error'}",
        )

    mode = stat.S_IMODE(file_stat.st_mode)
    common = {
        "path": cookie_path,
        "exists": True,
        "is_file": stat.S_ISREG(file_stat.st_mode),
        "mode": mode,
        "world_readable": bool(mode & 0o004),
    }
    if not common["is_file"]:
        return CookieFileCheck(**common, error="not a regular file")

    readable = os.access(cookie_path, os.R_OK)
    if not readable:
        return CookieFileCheck(**common, readable=False, error="file is not readable")

    try:
        data = cookie_path.read_bytes()
    except OSError as exc:
        return CookieFileCheck(
            **common,
            readable=False,
            error=f"cannot read file: {exc.strerror or 'OS error'}",
        )

    return _inspect_cookie_bytes(data, readable=readable, **common)


def _inspect_cookie_bytes(
    data: bytes,
    *,
    path: Path,
    exists: bool,
    is_file: bool,
    readable: bool,
    mode: int | None,
    world_readable: bool,
) -> CookieFileCheck:
    has_header = False
    data_lines = 0
    error: str | None = None

    for line_number, raw_line in enumerate(data.splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue

        is_http_only = line.lower().startswith(b"#httponly_")
        if line.startswith(b"#") and not is_http_only:
            lowered = line.lower()
            if data_lines == 0 and b"http cookie file" in lowered:
                has_header = True
            continue

        fields = line.split(b"\t")
        if len(fields) != 7:
            error = (
                f"line {line_number}: expected 7 tab-separated fields, "
                f"got {len(fields)}"
            )
            break
        if is_http_only:
            fields[0] = fields[0][len("#HttpOnly_") :]
        invalid = _validate_cookie_fields(fields)
        if invalid is not None:
            error = f"line {line_number}: {invalid}"
            break
        data_lines += 1

    if error is None and not has_header:
        error = "missing '# Netscape HTTP Cookie File' header"
    if error is None and data_lines == 0:
        error = "no cookie records found"

    return CookieFileCheck(
        path=path,
        exists=exists,
        is_file=is_file,
        readable=readable,
        mode=mode,
        world_readable=world_readable,
        has_header=has_header,
        format_ok=error is None and data_lines > 0,
        data_lines=data_lines,
        error=error,
    )


def _validate_cookie_fields(fields: list[bytes]) -> str | None:
    domain, include_subdomains, cookie_path, secure, expires, name, _value = fields
    if not domain:
        return "empty domain"
    if include_subdomains.upper() not in {b"TRUE", b"FALSE"}:
        return "include-subdomains flag must be TRUE or FALSE"
    if not cookie_path:
        return "empty path"
    if secure.upper() not in {b"TRUE", b"FALSE"}:
        return "secure flag must be TRUE or FALSE"
    try:
        int(expires)
    except ValueError:
        return "expiry must be an integer"
    if not name:
        return "empty cookie name"
    return None


def validate_cookie_file(
    path: str | Path,
    *,
    require_private: bool = False,
) -> CookieFileCheck:
    """Validate a cookie file, raising a typed, content-free error."""
    result = inspect_cookie_file(path)
    if not result.exists:
        raise CookieError(f"cookies file not found: {result.path}")
    if not result.is_file:
        raise CookieError(f"cookies path is not a regular file: {result.path}")
    if not result.readable:
        raise CookieError(f"cookies file is not readable: {result.path}")
    if require_private and result.world_readable:
        mode = f"{result.mode:03o}" if result.mode is not None else "unknown"
        raise CookieError(
            f"cookies file is world-readable ({mode}): {result.path}; "
            "run `chmod 600` on it"
        )
    if not result.format_ok:
        raise CookieError(
            f"cookies file is not valid Netscape format: {result.path}: "
            f"{result.error or 'unknown format error'}"
        )
    return result


def resolve_proxy(
    value: str | None,
    *,
    env: Mapping[str, str] | None = None,
    label: str = "proxy",
) -> str | None:
    """Resolve a profile proxy setting to a URL or ``None`` (direct).

    ``"none"`` means explicitly direct, ``"env"`` reads the conventional
    proxy variables.  A missing environment proxy is treated as direct.
    """
    environment = os.environ if env is None else env
    if value is None:
        return None
    normalized = value.strip()
    if not normalized:
        return None
    lowered = normalized.lower()
    if lowered == "none":
        return None
    if lowered == "env":
        for key in _PROXY_ENV_KEYS:
            candidate = environment.get(key)
            if candidate and candidate.strip():
                return validate_proxy_url(
                    candidate,
                    label=f"{label} from {key}",
                )
        return None
    return validate_proxy_url(normalized, label=label)


def validate_proxy_url(value: str, *, label: str = "proxy") -> str:
    """Validate a proxy URL without connecting to it."""
    candidate = value.strip()
    try:
        parsed = urlsplit(candidate)
        port = parsed.port
    except ValueError:
        raise ConfigError(
            f"{label} is not a valid URL: {redact_proxy(candidate)}"
        ) from None
    if parsed.scheme.lower() not in _PROXY_SCHEMES:
        raise ConfigError(
            f"{label} must use one of {', '.join(sorted(_PROXY_SCHEMES))}; "
            f"got {redact_proxy(candidate)}"
        )
    if not parsed.hostname:
        raise ConfigError(
            f"{label} has no host: {redact_proxy(candidate)}"
        )
    if port is not None and not 1 <= port <= 65535:
        raise ConfigError(
            f"{label} has an invalid port: {redact_proxy(candidate)}"
        )
    return candidate


def ensure_auth_ready(
    profile: SourceProfile,
    *,
    source: str | None = None,
    cookies_required: bool = False,
) -> None:
    """Guard a network call with a typed auth error.

    A configured cookie file is validated offline.  Browser cookies are
    accepted as configured; reading the browser keyring is deferred to
    yt-dlp.  When ``cookies_required`` is true, a profile with neither method
    raises :class:`AuthRequired` instead of silently going to the network.
    """
    if profile.cookies_file is not None:
        validate_cookie_file(profile.cookies_file, require_private=True)
        return
    if profile.cookies_from_browser:
        return
    if cookies_required:
        label = source or profile.name
        raise AuthRequired(
            f"{label} requires cookies; configure "
            f"[sources.{label}].cookies or cookies_from_browser "
            "and run `tfg doctor --source " + label + "`"
        )
