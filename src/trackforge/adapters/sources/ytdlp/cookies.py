"""Per-site Netscape cookie file selection.

Cookie files contain live session secrets.  This module only returns a path
and validates metadata (regular file, 0600).  It never reads, logs, or copies
cookie values.  The doctor check may parse the expiry column, but keeps it
inside the check and reports only a boolean/count.
"""

from __future__ import annotations

import stat
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
    "CookieSelection",
    "cookie_file_expired",
    "cookie_file_matches",
    "cookie_permissions_ok",
    "select_cookie_file",
]
