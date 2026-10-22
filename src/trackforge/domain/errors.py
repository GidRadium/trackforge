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


class RateLimited(TrackForgeError):
    """Raised when a provider rejects a request due to rate limiting."""

    default_retryable = True
    default_user_action = "Wait, then retry with a lower request rate."


class ResolutionFailed(TrackForgeError):
    """Raised when a field cannot be resolved under the active policy."""

    default_user_action = "Review the claims and field policy."


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


# Convenient aliases used by callers that prefer shorter spellings.
ValidationError = DomainValidationError
InvalidTransition = InvalidStateTransition

__all__ = [
    "AcquisitionFailed",
    "CandidateNotFound",
    "ConfigError",
    "DomainValidationError",
    "InvalidId",
    "InvalidStateTransition",
    "InvalidTransition",
    "PluginError",
    "ProviderUnavailable",
    "PublishFailed",
    "RateLimited",
    "RenderFailed",
    "ResolutionFailed",
    "ReviewRequired",
    "TrackForgeError",
    "UnsupportedScope",
    "UnsupportedSource",
    "ValidationError",
]
