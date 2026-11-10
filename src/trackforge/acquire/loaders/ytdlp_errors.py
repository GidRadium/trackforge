"""Translate yt-dlp exceptions into typed acquisition errors.

yt-dlp's exception types are stable enough to be handled by class, but the
specific reason is only present in the message.  We therefore use the message
*only* for classification and display, never as a control-flow protocol.
"""

# pyright: standard

from __future__ import annotations

import re
from collections.abc import Iterable
from types import TracebackType

from ..errors import (
    AcquireError,
    AuthRequired,
    DownloadFailed,
    DrmProtected,
    GeoBlocked,
    NoFormats,
    RateLimited,
    Unavailable,
    UnsupportedUrl,
)
from ..redaction import redact_text

_ERROR_PREFIX_RE = re.compile(r"^\s*(?:ERROR:\s*)+", re.IGNORECASE)
_WHITESPACE_RE = re.compile(r"\s+")

_GEO_PATTERNS = (
    "not available in your country",
    "not available in your region",
    "not available in your location",
    "blocked in your country",
    "blocked in your region",
    "geo-restricted",
    "geo restricted",
    "geo-blocked",
    "geo blocked",
    "region-locked",
    "region locked",
    "in your country",
)
_AUTH_PATTERNS = (
    "sign in",
    "sign-in",
    "logged in",
    "log in",
    "login",
    "not a bot",
    "confirm you're not a bot",
    "confirm you are not a bot",
    "cookies",
    "authentication",
    "authenticate",
    "private video",
    "this video is private",
    "private playlist",
    "playlist is private",
    "this playlist is private",
    "http error 401",
    "http error 403",
    "forbidden",
    "access denied",
    "members-only",
    "members only",
    "member-only",
    "membership",
    "premium only",
    "premium-only",
    "premium member",
    "members",
    "subscriber only",
    "subscriber-only",
    "age-restricted",
    "age restricted",
    "confirm your age",
)
_DRM_PATTERNS = (
    "drm",
    "digital rights management",
    "widevine",
    "fairplay",
    "playready",
    "has_drm",
    "protected by drm",
)
_RATE_PATTERNS = (
    "http error 429",
    "429 too many requests",
    "too many requests",
    "rate limit",
    "rate-limit",
    "ratelimit",
    "temporarily blocked",
    "try again later",
)
_UNSUPPORTED_PATTERNS = (
    "unsupported url",
    "no suitable extractor",
    "no extractor",
    "is not a valid url",
    "not a valid url",
)
_NO_FORMATS_PATTERNS = (
    "no video formats found",
    "no formats found",
    "no downloadable formats",
    "requested format is not available",
    "no format found",
)
_UNAVAILABLE_PATTERNS = (
    "video unavailable",
    "this video is unavailable",
    "content is not available",
    "not available",
    "has been removed",
    "was removed",
    "has been deleted",
    "was deleted",
    "does not exist",
    "no longer available",
    "account associated with this video has been terminated",
    "this video has been removed",
    "http error 404",
    "404 not found",
    "not found",
    "copyright",
)


def _collect_text(exc: BaseException) -> str:
    """Collect the useful parts of a yt-dlp exception chain."""
    parts: list[str] = []
    seen: set[int] = set()

    def add(text: str) -> None:
        if not text:
            return
        # A wrapped exception often repeats its cause.  Keep the longest
        # version and drop shorter substrings to avoid duplicate messages.
        if any(text == existing or text in existing for existing in parts):
            return
        parts[:] = [existing for existing in parts if existing not in text]
        parts.append(text)

    def visit(current: object) -> None:
        if current is None or id(current) in seen:
            return
        seen.add(id(current))
        if isinstance(current, (type, TracebackType)):
            return
        if isinstance(current, str):
            add(current)
            return
        if isinstance(current, BaseException):
            add(str(current))
            message = getattr(current, "msg", None)
            if isinstance(message, str):
                add(message)
            for attribute in ("exc_info", "cause", "__cause__", "__context__"):
                visit(getattr(current, attribute, None))
            return
        if isinstance(current, tuple):
            for entry in current:
                visit(entry)
            return
        # Arbitrary objects (traceback frames, sentinels) are not useful.

    visit(exc)
    return " ".join(part for part in parts if part).strip()


def humanize_error(exc: BaseException | str) -> str:
    """Return a short, redacted, single-line error message for users."""
    text = _collect_text(exc) if isinstance(exc, BaseException) else str(exc)
    text = _ERROR_PREFIX_RE.sub("", text)
    text = _WHITESPACE_RE.sub(" ", text).strip()
    text = redact_text(text)
    if len(text) > 500:
        text = f"{text[:497]}..."
    return text or "yt-dlp failed"


def _contains(text: str, patterns: Iterable[str]) -> bool:
    lowered = text.lower()
    return any(pattern in lowered for pattern in patterns)


def classify_ytdlp_error(exc: BaseException | str) -> str:
    """Return a stable category name for an exception/message."""
    text = _collect_text(exc) if isinstance(exc, BaseException) else str(exc)
    if _contains(text, _DRM_PATTERNS):
        return "drm"
    if _contains(text, _GEO_PATTERNS):
        return "geo"
    if _contains(text, _AUTH_PATTERNS):
        return "auth"
    if _contains(text, _RATE_PATTERNS):
        return "rate"
    if _contains(text, _UNSUPPORTED_PATTERNS):
        return "unsupported"
    if _contains(text, _NO_FORMATS_PATTERNS):
        return "formats"
    if _contains(text, _UNAVAILABLE_PATTERNS):
        return "unavailable"
    return "failed"


def map_ytdlp_error(
    exc: BaseException,
    *,
    url: str | None = None,
) -> AcquireError:
    """Map ``DownloadError``/``ExtractorError`` to a typed ``AcquireError``."""
    category = classify_ytdlp_error(exc)
    message = humanize_error(exc)
    if category == "drm":
        return DrmProtected(message)
    if category == "geo":
        return GeoBlocked(message)
    if category == "auth":
        return AuthRequired(message)
    if category == "rate":
        return RateLimited(message)
    if category == "unsupported":
        return UnsupportedUrl(url or "", hint=message)
    if category == "formats":
        return NoFormats(message)
    if category == "unavailable":
        return Unavailable(message)
    return DownloadFailed(message)


def is_retryable(exc: BaseException) -> bool:
    """Whether the mapped acquisition error is worth retrying."""
    return map_ytdlp_error(exc).retryable


__all__ = [
    "classify_ytdlp_error",
    "humanize_error",
    "is_retryable",
    "map_ytdlp_error",
]
