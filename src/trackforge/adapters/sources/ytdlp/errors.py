"""Map yt-dlp subprocess failures to typed TrackForge errors.

The mapper is intentionally text-based: yt-dlp exposes a stable CLI, not a
machine-readable error taxonomy.  Patterns are matched case-insensitively and
ordered from the most specific condition to the generic extractor failure.
Messages are redacted before they enter an error context, so URL query tokens
and cookie values never reach logs or JSON output.
"""

from __future__ import annotations

import re

from trackforge.adapters.http.url_safety import redact_text, sanitize_url
from trackforge.domain.errors import (
    AcquisitionFailed,
    CookiesExpired,
    CookiesPermissionsError,
    DownloaderNotFound,
    DownloadTimeout,
    DrmProtected,
    ExtractorBroken,
    GeoBlocked,
    LoginRequired,
    PreviewOnly,
    ProviderUnavailable,
    RateLimited,
    TrackForgeError,
    Unavailable,
    UnsupportedUrl,
)

_MAX_STDERR_CHARS = 8_192

_LOGIN_RE = re.compile(
    r"(sign in|log ?in|login required|authentication|unauthorized|"
    r"http error 40[13]|age[- ]?restricted|"
    r"confirm you(?:'| a)?re not a bot|bot check|use --cookies|cookies for the authentication)",
    re.IGNORECASE,
)
_EXPIRED_RE = re.compile(
    r"(cookie[^\n]{0,80}(?:expired|stale|invalid|no longer)|"
    r"(?:expired|stale|invalid)[^\n]{0,80}cookie)",
    re.IGNORECASE,
)
_RATE_RE = re.compile(
    r"(http error 429|status(?: code)? 429|\b429\b|too many requests|rate.?limit|"
    r"temporarily blocked)",
    re.IGNORECASE,
)
_GEO_RE = re.compile(
    r"(not available in your country|unavailable in your country|"
    r"geo[- ]?block|blocked in your (?:country|region)|"
    r"available only in|region[- ]?locked)",
    re.IGNORECASE,
)
_DRM_RE = re.compile(
    r"(\bdrm\b|digital rights management|protected by drm|drm[- ]protected|"
    r"unable to decrypt|encrypted (?:stream|content)|paid[- ](?:content|track|only)|premium only)",
    re.IGNORECASE,
)
_PREVIEW_RE = re.compile(r"(smart_preview|preview[- ]only|only preview|preview stream)", re.I)
_UNAVAILABLE_RE = re.compile(
    r"(video unavailable|private video|has been removed|does not exist|"
    r"account has been terminated|http error 404|status 404|"
    r"this video is private|this video is not available|content is not available|"
    r"content isn't available)",
    re.IGNORECASE,
)
_UNSUPPORTED_RE = re.compile(
    r"(unsupported url|no suitable extractor|is not a valid url|"
    r"no extractor found|unable to find a suitable extractor)",
    re.IGNORECASE,
)
_BROKEN_RE = re.compile(
    r"(unable to extract|failed to parse|no video formats|requested format is not available|"
    r"extractor error|json|keyerror|typeerror|noneType|subscriptable|"
    r"failed to decode|invalid character)",
    re.IGNORECASE,
)
_SERVER_RE = re.compile(r"(http error 5\d\d|status(?: code)? 5\d\d|\b5\d\d\b)", re.I)
# YouTube sometimes returns a short-lived 403 / "unable to download video
# data" for anonymous clients even when the same URL works on a later attempt.
# Keep genuine login/age-gate failures non-retryable, but let this specific
# Forbidden variant participate in the bounded download retry loop.
_TRANSIENT_FORBIDDEN_RE = re.compile(
    r"(http error 403|status(?: code)? 403|\b403\b|forbidden|"
    r"unable to download video data)",
    re.IGNORECASE,
)
_TIMEOUT_RE = re.compile(r"(timed out|timeout|read timeout|connection reset)", re.I)
_PRIVATE_RE = re.compile(
    r"(private video|video is private|video unavailable|has been removed|"
    r"account has been terminated|does not exist)",
    re.IGNORECASE,
)


def redact_stderr(stderr: str | None, *, max_chars: int = _MAX_STDERR_CHARS) -> str:
    """Return a bounded, redacted copy of downloader stderr."""

    text = (stderr or "").strip()
    if not text:
        return ""
    text = redact_text(text)
    # A cookie line should never be echoed; keep the fact that one appeared.
    lines: list[str] = []
    for line in text.splitlines():
        lowered = line.casefold()
        if "cookie:" in lowered or "set-cookie:" in lowered:
            lines.append("<cookie data redacted>")
        else:
            lines.append(line)
    joined = "\n".join(lines)
    return joined[-max_chars:]


def classify_error(
    stderr: str | None,
    *,
    returncode: int | None = None,
) -> type[TrackForgeError]:
    """Return the best typed error class for one yt-dlp failure."""

    text = stderr or ""
    if _EXPIRED_RE.search(text):
        return CookiesExpired
    if _PREVIEW_RE.search(text):
        return PreviewOnly
    if _TIMEOUT_RE.search(text):
        return DownloadTimeout
    if _GEO_RE.search(text):
        return GeoBlocked
    if _DRM_RE.search(text):
        return DrmProtected
    if _PRIVATE_RE.search(text):
        return Unavailable
    if _LOGIN_RE.search(text):
        return LoginRequired
    if _RATE_RE.search(text):
        return RateLimited
    if _UNSUPPORTED_RE.search(text):
        return UnsupportedUrl
    if _BROKEN_RE.search(text):
        return ExtractorBroken
    if _UNAVAILABLE_RE.search(text):
        return Unavailable
    if _SERVER_RE.search(text):
        return ProviderUnavailable
    if returncode is not None and returncode < 0:
        return ExtractorBroken
    return ExtractorBroken


def map_error(
    stderr: str | None,
    *,
    returncode: int | None = None,
    url: str | None = None,
    max_stderr_chars: int = _MAX_STDERR_CHARS,
) -> TrackForgeError:
    """Build a typed error with a redacted, bounded context."""

    error_type = classify_error(stderr, returncode=returncode)
    safe_stderr = redact_stderr(stderr, max_chars=max_stderr_chars)
    context: dict[str, object] = {
        "returncode": returncode,
        "stderr": safe_stderr,
    }
    if url is not None:
        context["url"] = sanitize_url(url)
    if error_type is ExtractorBroken and "requested format is not available" in (
        safe_stderr.casefold()
    ):
        return ExtractorBroken(
            "The selected audio format is unavailable; yt-dlp may be missing a "
            "JavaScript challenge solver or the format changed",
            user_action=(
                "Install the downloader extras (yt-dlp[default], yt-dlp-ejs) and "
                "Deno, then run `yt-dlp --list-formats` for this URL."
            ),
            context=context,
        )
    message = _message_for(error_type, safe_stderr)
    retryable = (
        True
        if error_type is LoginRequired and _TRANSIENT_FORBIDDEN_RE.search(safe_stderr)
        else None
    )
    return error_type(message, context=context, retryable=retryable)


def map_ytdlp_error(
    stderr: str | None,
    *,
    returncode: int | None = None,
    url: str | None = None,
    max_stderr_chars: int = _MAX_STDERR_CHARS,
) -> TrackForgeError:
    """Alias for :func:`map_error` used by adapters and tests."""

    return map_error(
        stderr,
        returncode=returncode,
        url=url,
        max_stderr_chars=max_stderr_chars,
    )


def _message_for(error_type: type[TrackForgeError], stderr: str) -> str:
    last_line = next(
        (line.strip() for line in reversed(stderr.splitlines()) if line.strip()),
        "",
    )
    detail = f": {last_line}" if last_line else ""
    if issubclass(error_type, CookiesExpired):
        return f"The source rejected the configured cookies{detail}"
    if issubclass(error_type, LoginRequired):
        return f"The source requires authentication or a valid cookie session{detail}"
    if issubclass(error_type, RateLimited):
        return f"The source rate-limited the request{detail}"
    if issubclass(error_type, DownloadTimeout):
        return f"The downloader timed out{detail}"
    if issubclass(error_type, GeoBlocked):
        return f"The source is not available in this region{detail}"
    if issubclass(error_type, DrmProtected):
        return f"The source is DRM protected or paid-only{detail}"
    if issubclass(error_type, UnsupportedUrl):
        return f"yt-dlp does not support this URL{detail}"
    if issubclass(error_type, Unavailable):
        return f"The source is unavailable, private, or removed{detail}"
    if issubclass(error_type, PreviewOnly):
        return f"Only a preview stream was downloaded{detail}"
    if issubclass(error_type, ProviderUnavailable):
        return f"The source is temporarily unavailable{detail}"
    return f"yt-dlp could not extract the source{detail}"


def is_retryable(error: TrackForgeError) -> bool:
    """Return whether the error policy recommends a later retry."""

    if isinstance(error, (RateLimited, DownloadTimeout, ProviderUnavailable)):
        return True
    return error.retryable


# Convenient aliases for callers that prefer explicit names.
YtDlpError = AcquisitionFailed
MissingDownloader = DownloaderNotFound
CookiePermissionError = CookiesPermissionsError

__all__ = [
    "CookiePermissionError",
    "MissingDownloader",
    "YtDlpError",
    "classify_error",
    "is_retryable",
    "map_error",
    "map_ytdlp_error",
    "redact_stderr",
]
