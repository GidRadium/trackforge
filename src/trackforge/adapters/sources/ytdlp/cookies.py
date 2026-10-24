"""Per-site Netscape cookie file selection.

Cookie files contain live session secrets.  This module only returns a path
and validates metadata (regular file, 0600).  It never reads, logs, or copies
cookie values.  The doctor check may parse the expiry column, but keeps it
inside the check and reports only a boolean/count.
"""

from __future__ import annotations

import stat
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from trackforge.adapters.http.url_safety import parse_http_url
from trackforge.domain.errors import CookiesPermissionsError, UnsafeUrl


@dataclass(frozen=True, slots=True)
class CookieSelection:
    """A cookie file selected for one host."""

    path: Path
    host: str

    def __post_init__(self) -> None:
        if not self.path.is_absolute():
            raise ValueError("cookie path must be absolute")


@dataclass(frozen=True, slots=True)
class CookieRoute:
    """A configured cookie file for one host suffix.

    ``path=None`` means cookies are explicitly disabled for the route; this
    is different from "no route" and therefore suppresses domain fallback.
    """

    host_suffix: str
    path: Path | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.host_suffix, str):
            raise ValueError("cookie route host must be a string")
        text = self.host_suffix.strip().casefold().rstrip(".")
        if text.startswith("*."):
            text = text[2:]
        if (
            not text
            or "/" in text
            or ":" in text
            or "*" in text
            or any(char.isspace() for char in text)
        ):
            raise ValueError("cookie route host must be a bare hostname or suffix")
        if self.path is not None:
            if not isinstance(self.path, Path):
                raise ValueError("cookie route path must be a Path or None")
        object.__setattr__(self, "host_suffix", text)

    def matches(self, host: str) -> bool:
        candidate = host.strip().casefold().rstrip(".")
        return candidate == self.host_suffix or candidate.endswith(f".{self.host_suffix}")


@dataclass(frozen=True, slots=True)
class CookieResolver:
    """Configured cookie routes with a domain-based fallback.

    Configured routes are checked first and use host-suffix matching.  If no
    route matches, the legacy ``<cookie_dir>/<host>.txt`` lookup is used, so a
    site supported by yt-dlp but not yet known to TrackForge still works.
    """

    cookie_dir: Path | None = None
    routes: tuple[CookieRoute, ...] = ()
    base_dir: Path | None = None

    def __post_init__(self) -> None:
        routes = tuple(self.routes)
        for route in routes:
            if not isinstance(route, CookieRoute):
                raise ValueError("routes must be CookieRoute values")
        routes = tuple(
            sorted(
                routes,
                key=lambda item: (len(item.host_suffix.split(".")), len(item.host_suffix)),
                reverse=True,
            )
        )
        base_dir = self.base_dir
        if base_dir is None and self.cookie_dir is not None:
            base_dir = self.cookie_dir.parent
        object.__setattr__(self, "routes", routes)
        object.__setattr__(self, "base_dir", base_dir)

    @classmethod
    def from_mapping(
        cls,
        cookie_dir: Path | None,
        routes: Mapping[str, str] | None,
        *,
        base_dir: Path | None = None,
    ) -> CookieResolver:
        parsed: list[CookieRoute] = []
        for raw_host, raw_path in (routes or {}).items():
            text = str(raw_path).strip()
            parsed.append(
                CookieRoute(
                    raw_host,
                    None if not text else Path(text).expanduser(),
                )
            )
        return cls(cookie_dir=cookie_dir, routes=tuple(parsed), base_dir=base_dir)

    def route_for(self, url: str) -> CookieRoute | None:
        try:
            host = parse_http_url(url).host
        except UnsafeUrl:
            return None
        return next((route for route in self.routes if route.matches(host)), None)

    def select(
        self,
        url: str,
        *,
        site_hint: str | None = None,
    ) -> CookieSelection | None:
        route = self.route_for(url)
        if route is not None:
            if route.path is None:
                return None
            path = self._resolve(route.path)
            try:
                _require_safe_cookie_file(path)
            except CookiesPermissionsError:
                raise
            except OSError as exc:
                raise CookiesPermissionsError(
                    "configured cookie file cannot be read",
                    user_action=(
                        "Check [network.ytdlp.cookies] or remove the route so "
                        "domain-based discovery can be used."
                    ),
                    context={"path": str(path), "error": str(exc)},
                ) from exc
            host = parse_http_url(url).host
            return CookieSelection(path=path.resolve(), host=host)
        return select_cookie_file(url, self.cookie_dir, site_hint=site_hint)

    def _resolve(self, path: Path) -> Path:
        candidate = path.expanduser()
        if candidate.is_absolute():
            return candidate
        base = self.base_dir or Path.cwd()
        return base / candidate


def _candidate_stems(host: str, site_hint: str | None) -> tuple[str, ...]:
    raw: list[str] = []
    if site_hint:
        hint = site_hint.strip().lower().rstrip(".")
        if hint:
            raw.append(hint)
    normalized = host.strip().lower().rstrip(".")
    raw.append(normalized)
    if normalized.startswith("www."):
        raw.append(normalized[4:])
    parts = normalized.split(".")
    # Prefer the most specific name but never fall back to a bare TLD.
    for index in range(1, len(parts) - 1):
        raw.append(".".join(parts[index:]))
    result: list[str] = []
    for value in raw:
        value = value.replace("/", "_")
        if value and value not in result:
            result.append(value)
    return tuple(result)


def select_cookie_file(
    url: str,
    cookie_dir: Path | None,
    *,
    site_hint: str | None = None,
) -> CookieSelection | None:
    """Return the most specific ``<site>.txt`` cookie file for ``url``.

    A missing directory or file is not an error: many public sources work
    without cookies.  A file with permissions other than 0600 is an error and
    must not be used.
    """

    if cookie_dir is None:
        return None
    directory = Path(cookie_dir)
    if not directory.is_dir():
        return None
    try:
        host = parse_http_url(url).host
    except UnsafeUrl:
        return None
    stems = _candidate_stems(host, site_hint)
    by_name = {entry.name.casefold(): entry for entry in directory.iterdir() if entry.is_file()}
    for stem in stems:
        entry = by_name.get(f"{stem}.txt")
        if entry is None:
            continue
        _require_safe_cookie_file(entry)
        return CookieSelection(path=entry.resolve(), host=host)
    return None


def cookie_file_matches(path: Path | None, url: str, *, site_hint: str | None = None) -> bool:
    """Return whether ``path`` is a cookie file for the URL's host."""

    if path is None:
        return False
    try:
        host = parse_http_url(url).host
    except UnsafeUrl:
        return False
    stem = Path(path).name.casefold()
    if stem.endswith(".txt"):
        stem = stem[:-4]
    return stem in _candidate_stems(host, site_hint)


def _require_safe_cookie_file(path: Path) -> None:
    if path.is_symlink():
        raise CookiesPermissionsError(
            "cookie files must not be symlinks",
            user_action="Replace the symlink with a regular cookie file mode 0600.",
            context={"path": str(path)},
        )
    metadata = path.stat()
    if not stat.S_ISREG(metadata.st_mode):
        raise CookiesPermissionsError(
            "cookie path is not a regular file",
            context={"path": str(path)},
        )
    mode = stat.S_IMODE(metadata.st_mode)
    if mode != 0o600:
        raise CookiesPermissionsError(
            "cookie file permissions must be 0600",
            user_action=f"Run: chmod 600 {path}",
            context={"path": str(path), "mode": f"{mode:04o}"},
        )


def cookie_permissions_ok(path: Path) -> bool:
    """Return whether a cookie file is a regular, non-symlink 0600 file."""

    try:
        _require_safe_cookie_file(path)
    except (CookiesPermissionsError, OSError):
        return False
    return True


def cookie_file_expired(path: Path, *, now: datetime | None = None) -> bool:
    """Parse Netscape expiry columns and return whether every cookie expired.

    An empty file or a file with session cookies (expiry 0) is not considered
    expired.  Values are never returned or logged.
    """

    current = (now or datetime.now(UTC)).timestamp()
    expiries: list[int] = []
    try:
        with Path(path).open("r", encoding="utf-8", errors="replace") as stream:
            for line in stream:
                text = line.strip()
                if not text or text.startswith("#"):
                    continue
                columns = text.split("\t")
                if len(columns) < 6:
                    continue
                try:
                    expiry = int(columns[4])
                except ValueError:
                    continue
                if expiry > 0:
                    expiries.append(expiry)
    except OSError:
        return False
    return bool(expiries) and all(expiry <= current for expiry in expiries)


__all__ = [
    "CookieResolver",
    "CookieRoute",
    "CookieSelection",
    "cookie_file_expired",
    "cookie_file_matches",
    "cookie_permissions_ok",
    "select_cookie_file",
]
