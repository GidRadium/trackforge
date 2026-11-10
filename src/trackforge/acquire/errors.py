"""Typed acquisition errors.

Every error carries a ``retryable`` policy so callers (and the CLI) do not
have to inspect exception messages.  ``hint`` is a short, user-facing action;
it deliberately contains no secrets.
"""

# pyright: standard

from __future__ import annotations


class AcquireError(Exception):
    """Base class for all acquisition failures."""

    default_message = "acquisition failed"
    retryable = False
    hint: str | None = None

    def __init__(self, message: str | None = None, *, hint: str | None = None) -> None:
        super().__init__(message or self.default_message)
        if hint is not None:
            self.hint = hint


class UnsupportedInput(AcquireError):
    """The input is neither a URL, an existing path nor a text query."""

    default_message = "unsupported input"
    hint = "pass an http(s) URL, an existing path, or a text query"


class UnsupportedScheme(AcquireError):
    """The input uses a URL scheme we do not handle."""

    default_message = "unsupported URL scheme"
    hint = "only http and https URLs are supported"


class UnsupportedUrl(AcquireError):
    """No registered loader can handle this URL."""

    default_message = "unsupported URL"
    hint = "try `tfg search` or choose a loader with --source"

    def __init__(
        self,
        url: str,
        *,
        source: str | None = None,
        hint: str | None = None,
    ) -> None:
        message = f"unsupported URL: {url}"
        if source:
            message = f"{message} (source {source!r})"
        super().__init__(message, hint=hint)
        self.url = url
        self.source = source


class AmbiguousInput(AcquireError):
    """The input could refer to more than one item."""

    default_message = "ambiguous input"
    hint = "provide a URL or pick a concrete item"


class SearchUnavailable(AcquireError):
    """Search is temporarily unavailable."""

    default_message = "search is unavailable"
    retryable = True
    hint = "retry later or use a direct URL"


class AuthRequired(AcquireError):
    """The source requires authentication (cookies, token or login)."""

    default_message = "authentication is required"
    hint = "configure cookies/token for this source; see `tfg doctor`"


class CookieError(AcquireError):
    """Cookies are missing, unreadable, malformed or expired."""

    default_message = "cookies are not usable"
    hint = "refresh the cookies file configured for this source"


class ProxyError(AcquireError):
    """The configured proxy could not be used."""

    default_message = "proxy is not usable"
    retryable = True
    hint = "check the proxy URL in the source profile"


class GeoBlocked(AcquireError):
    """The content is not available in the current region."""

    default_message = "content is geo-blocked"
    hint = "try a proxy in a supported region"


class DrmProtected(AcquireError):
    """The media is DRM-protected and cannot be downloaded."""

    default_message = "media is DRM-protected"
    hint = "DRM-protected media cannot be downloaded"


class RateLimited(AcquireError):
    """The source refused the request because of rate limiting."""

    default_message = "rate limited by the source"
    retryable = True
    hint = "wait before retrying, or configure a proxy"


class Unavailable(AcquireError):
    """The media was removed, made private or is otherwise unavailable."""

    default_message = "media is unavailable"
    hint = "it may have been deleted or made private"


class NoFormats(AcquireError):
    """The source exposes no downloadable formats."""

    default_message = "no downloadable formats"
    hint = "refresh the item or try another source"


class VariantUnavailable(AcquireError):
    """The selected variant/format no longer exists."""

    default_message = "selected variant is unavailable"
    hint = "refresh the item and choose a variant again"


class DownloadFailed(AcquireError):
    """The download failed for a transient or source-specific reason."""

    default_message = "download failed"
    retryable = True
    hint = "retry; if it keeps failing, refresh the item"


class PostprocessError(AcquireError):
    """Merge/remux/convert post-processing failed."""

    default_message = "post-processing failed"
    hint = "try another variant or check that ffmpeg is installed"


class CancelledError(AcquireError):
    """The operation was cancelled by the caller."""

    default_message = "operation cancelled"


class ConfigError(AcquireError):
    """The configuration is invalid or incomplete."""

    default_message = "invalid configuration"
    hint = "check the config file and environment variables"


class SearchNotSupported(AcquireError):
    """A loader has no search implementation."""

    default_message = "search is not supported"
    hint = "use a direct URL instead of a text search"

    def __init__(self, source: str | None = None, *, hint: str | None = None) -> None:
        message = self.default_message
        if source:
            message = f"{message} by loader {source!r}"
        super().__init__(message, hint=hint)
        self.source = source
