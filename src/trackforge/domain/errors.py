"""Typed domain errors with safe, JSON-serializable context.

The domain layer must not leak framework exceptions to callers.  Every error
carries whether a retry can help, an optional user-facing action, and a
redacted context dictionary suitable for logs or ``--json`` output.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from datetime import date, datetime, time
from enum import Enum
from pathlib import Path
from typing import Any

_SENSITIVE_KEY_PARTS = (
    "api_key",
    "apikey",
    "authorization",
    "cookie",
    "credential",
    "password",
    "passwd",
    "private_key",
    "secret",
    "session_id",
    "token",
)
_MAX_VALUE_LENGTH = 2_000


def _is_sensitive_key(key: str) -> bool:
    normalized = key.strip().lower().replace("-", "_").replace(" ", "_")
    return any(part in normalized for part in _SENSITIVE_KEY_PARTS)


def _json_safe(value: Any, *, depth: int = 0, seen: set[int] | None = None) -> Any:
    """Return a JSON-compatible, redacted copy of ``value``."""

    if depth > 20:
        return "<truncated>"
    if seen is None:
        seen = set()

    if isinstance(value, Enum):
        return _json_safe(value.value, depth=depth + 1, seen=seen)
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return f"<binary:{len(value)} bytes>"
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()

    if isinstance(value, Mapping):
        identifier = id(value)
        if identifier in seen:
            return "<circular>"
        seen.add(identifier)
        try:
            return _redact_mapping(value, depth=depth + 1, seen=seen)
        finally:
            seen.discard(identifier)

    if isinstance(value, (list, tuple, set, frozenset)):
        identifier = id(value)
        if identifier in seen:
            return "<circular>"
        seen.add(identifier)
        try:
            return [_json_safe(item, depth=depth + 1, seen=seen) for item in value]
        finally:
            seen.discard(identifier)

    if isinstance(value, BaseException):
        return f"<{type(value).__name__}: {value}>"

    text = str(value)
    if len(text) > _MAX_VALUE_LENGTH:
        return f"{text[:_MAX_VALUE_LENGTH]}…"
    return text


def _redact_mapping(
    value: Mapping[Any, Any],
    *,
    depth: int = 0,
    seen: set[int] | None = None,
) -> dict[str, Any]:
    redacted: dict[str, Any] = {}
    for raw_key, raw_value in value.items():
        key = raw_key if isinstance(raw_key, str) else str(raw_key)
        if _is_sensitive_key(key):
            redacted[key] = "***"
        else:
            redacted[key] = _json_safe(raw_value, depth=depth, seen=seen)
    return redacted


class TrackForgeError(Exception):
    """Base class for all expected TrackForge failures."""

    default_retryable: bool = False
    default_user_action: str | None = None

    def __init__(
        self,
        message: str,
        retryable: bool | None = None,
        user_action: str | None = None,
        context: object | None = None,
    ) -> None:
        self.message = str(message)
        self.retryable = self.default_retryable if retryable is None else bool(retryable)
        if user_action is None:
            self.user_action = self.default_user_action
        else:
            self.user_action = str(user_action)
        if context is None:
            self.context: dict[str, Any] = {}
        elif isinstance(context, Mapping):
            self.context = _redact_mapping(context)
        else:
            self.context = {"value": _json_safe(context)}
        super().__init__(self.message)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-compatible representation with redacted context."""

        return {
            "type": type(self).__name__,
            "message": self.message,
            "retryable": self.retryable,
            "user_action": self.user_action,
            "context": self.context,
        }

    def to_json(self) -> str:
        """Serialize the error to a single JSON object."""

        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True)

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(message={self.message!r}, "
            f"retryable={self.retryable!r}, user_action={self.user_action!r})"
        )


class ConfigError(TrackForgeError):
    """Raised when configuration cannot be loaded or validated."""

    default_user_action = "Check the TrackForge configuration."


class UnsupportedSource(TrackForgeError):
    """Raised when no source adapter supports the input."""

    default_user_action = "Use a supported source or install a plugin."


class CandidateNotFound(TrackForgeError):
    """Raised when a source returns no candidate for the request."""

    default_user_action = "Check the source reference and try again."


class AcquisitionFailed(TrackForgeError):
    """Raised when media cannot be acquired."""

    default_user_action = "Check network access, cookies, and the source."


class ProviderUnavailable(TrackForgeError):
    """Raised when an external provider is temporarily unavailable."""

    default_retryable = True
    default_user_action = "Retry later or configure an alternative provider."


class ObservationFailed(TrackForgeError):
    """Raised when an observation run cannot produce its required artifacts."""

    default_user_action = "Inspect the observation run and its warnings."


class ProbeFailed(ObservationFailed):
    """Raised when the media bytes cannot be probed."""

    default_user_action = "Check that the file is a valid, readable audio file."


class RateLimited(TrackForgeError):
    """Raised when a provider rejects a request due to rate limiting."""

    default_retryable = True
    default_user_action = "Wait, then retry with a lower request rate."


class OfflineModeError(UnsupportedSource):
    """Raised when a network source is used while offline mode is enabled."""

    default_user_action = "Remove --offline or use a local file."


class UnsupportedUrl(UnsupportedSource):
    """Raised when a URL is syntactically valid but not a supported source."""

    default_user_action = "Use a supported single-track URL; playlists arrive in Plan 18."


class UnsafeUrl(UnsupportedUrl):
    """Raised when a URL fails the SSRF or scheme safety policy."""

    default_user_action = "Use a public http(s) URL; private and local addresses are blocked."


class LoginRequired(AcquisitionFailed):
    """Raised when the source requires an authenticated session or cookies."""

    default_user_action = "Export browser cookies to the configured cookies directory and retry."


class CookiesExpired(LoginRequired):
    """Raised when configured cookies are present but no longer accepted."""

    default_user_action = "Refresh the cookie file for this site, then retry."


class CookiesPermissionsError(UnsupportedSource):
    """Raised when a cookie file exists with unsafe permissions."""

    default_user_action = "Set the cookie file mode to 0600 (owner read/write only)."


class GeoBlocked(AcquisitionFailed):
    """Raised when the source is unavailable in the current region."""

    default_user_action = "Use a source available in your region."


class DrmProtected(AcquisitionFailed):
    """Raised when the source is DRM protected or paid-only."""

    default_user_action = "DRM-protected and paid content is not supported."


class Unavailable(AcquisitionFailed):
    """Raised when the source was removed, made private, or does not exist."""

    default_user_action = "Check that the URL is public and still available."


class ExtractorBroken(AcquisitionFailed):
    """Raised when yt-dlp cannot parse the source or returns invalid JSON."""

    default_user_action = "Update yt-dlp or the site plugin, then retry."


class DownloaderNotFound(AcquisitionFailed):
    """Raised when the configured downloader executable is missing."""

    default_user_action = "Install yt-dlp and ensure it is available on PATH."


class DownloadTimeout(AcquisitionFailed):
    """Raised when metadata or media download exceeds its timeout."""

    default_retryable = True
    default_user_action = "Retry later or increase the configured timeout."


class PreviewOnly(CookiesExpired):
    """Raised when a downloaded file is only a short preview of the track."""

    default_user_action = "Refresh cookies or check that the track is not preview-only."


class ResolutionFailed(TrackForgeError):
    """Raised when a field cannot be resolved under the active policy."""

    default_user_action = "Review the claims and field policy."


class UnsupportedMode(TrackForgeError):
    """Raised when a resolution mode is not implemented in this build."""

    default_user_action = (
        "Use policy mode; LLM-assisted, autopilot, and manual modes arrive in later plans."
    )


class RenderFailed(TrackForgeError):
    """Raised when rendering or read-back verification fails."""

    default_user_action = "Check render options, tools, and logs."


class PublishFailed(TrackForgeError):
    """Raised when a bundle cannot be published to the library."""

    default_user_action = "Check library permissions and available space."


class ReviewRequired(TrackForgeError):
    """Raised when a decision must be confirmed by the user."""

    default_user_action = "Review the item and confirm the suggested values."


class PluginError(TrackForgeError):
    """Raised for plugin discovery, manifest, or execution failures."""

    default_user_action = "Check the plugin manifest, version, and dependencies."


class DomainValidationError(TrackForgeError, ValueError):
    """Raised when a domain value violates an invariant."""

    default_user_action = "Check the provided domain data."


class InvalidId(DomainValidationError):
    """Raised when an identifier is not a valid UUID string."""


class InvalidStateTransition(DomainValidationError):
    """Raised when a state machine transition is not allowed."""


class UnsupportedScope(DomainValidationError):
    """Raised when a correction requests a scope not executable in v1."""

    default_user_action = "Use item scope; broader rules are not available yet."


class StorageError(TrackForgeError):
    """Base class for expected storage failures."""

    default_user_action = "Check the TrackForge data and cache directories."


class InvalidStorageKey(StorageError, ValueError):
    """Raised when a hash, cache key, or payload format is invalid."""

    default_user_action = "Use a 64-character lowercase SHA-256 key."


class ArtifactCorrupted(StorageError):
    """Raised when stored bytes do not match their recorded hash or schema."""

    default_user_action = "Run storage verification or recompute the artifact."


class PathEscapeError(ArtifactCorrupted, ValueError):
    """Raised when a path would leave its allowed root or use ``..``."""

    default_user_action = "Use paths relative to the configured storage root."


class ArtifactConflict(StorageError):
    """Raised when a cache key already refers to different payload bytes."""

    default_user_action = "Use a new cache key or remove the stale artifact explicitly."


class ArtifactNotFound(StorageError, FileNotFoundError):
    """Raised when an expected object is absent from the store."""

    default_user_action = "Recompute the missing artifact."


class CatalogError(StorageError):
    """Base class for expected SQLite catalog failures."""

    default_user_action = "Check the TrackForge catalog and data directory."


class CatalogLocked(CatalogError):
    """Raised when another process holds the data-directory lock."""

    default_user_action = (
        "Wait for the other TrackForge process to finish, or use a different data directory."
    )


class MigrationError(CatalogError):
    """Raised when the catalog schema cannot be created or upgraded."""

    default_user_action = "Check the catalog file or restore it from backup."


class DuplicateRecord(CatalogError, ValueError):
    """Raised when a unique catalog constraint rejects a write."""

    default_user_action = "Use a unique identifier or update the existing record."


class DuplicateContentHash(DuplicateRecord):
    """Raised when two media objects would share the same content hash."""


class DuplicateCacheKey(DuplicateRecord):
    """Raised when two artifacts would share the same cache key."""


class DuplicateLock(DuplicateRecord):
    """Raised when an item/field lock already exists."""


class ForeignKeyViolation(CatalogError, ValueError):
    """Raised when a catalog write references a missing row."""

    default_user_action = "Create the referenced record before writing this one."


class RecordNotFound(CatalogError, LookupError):
    """Raised when an operation requires a catalog row that does not exist."""

    default_user_action = "Refresh the item or run the operation again."


class InvalidCatalogData(CatalogError, ValueError):
    """Raised when persisted catalog data cannot be decoded safely."""

    default_user_action = "Run storage verification or restore the catalog from backup."


# Convenient aliases used by callers that prefer shorter spellings.
ValidationError = DomainValidationError
InvalidTransition = InvalidStateTransition

__all__ = [
    "AcquisitionFailed",
    "ArtifactConflict",
    "ArtifactCorrupted",
    "ArtifactNotFound",
    "CandidateNotFound",
    "CatalogError",
    "CatalogLocked",
    "ConfigError",
    "DomainValidationError",
    "DuplicateCacheKey",
    "DuplicateContentHash",
    "DuplicateLock",
    "CookiesExpired",
    "CookiesPermissionsError",
    "DownloadTimeout",
    "DownloaderNotFound",
    "DrmProtected",
    "DuplicateRecord",
    "ExtractorBroken",
    "ForeignKeyViolation",
    "GeoBlocked",
    "LoginRequired",
    "OfflineModeError",
    "PreviewOnly",
    "InvalidCatalogData",
    "InvalidId",
    "InvalidStateTransition",
    "InvalidStorageKey",
    "InvalidTransition",
    "MigrationError",
    "ObservationFailed",
    "PathEscapeError",
    "PluginError",
    "ProbeFailed",
    "ProviderUnavailable",
    "PublishFailed",
    "RateLimited",
    "RecordNotFound",
    "RenderFailed",
    "ResolutionFailed",
    "ReviewRequired",
    "StorageError",
    "TrackForgeError",
    "UnsupportedScope",
    "UnsupportedMode",
    "UnsupportedSource",
    "UnsupportedUrl",
    "UnsafeUrl",
    "ValidationError",
]
