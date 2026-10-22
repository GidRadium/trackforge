"""Typed error behavior, redaction, and JSON serialization."""

from __future__ import annotations

import json

from trackforge.domain.errors import (
    AcquisitionFailed,
    CandidateNotFound,
    ConfigError,
    DomainValidationError,
    InvalidId,
    InvalidStateTransition,
    PluginError,
    ProviderUnavailable,
    PublishFailed,
    RateLimited,
    RenderFailed,
    ResolutionFailed,
    ReviewRequired,
    TrackForgeError,
    UnsupportedSource,
)


def test_all_documented_errors_are_trackforge_errors() -> None:
    subclasses = [
        ConfigError,
        UnsupportedSource,
        CandidateNotFound,
        AcquisitionFailed,
        ProviderUnavailable,
        RateLimited,
        ResolutionFailed,
        RenderFailed,
        PublishFailed,
        ReviewRequired,
        PluginError,
    ]

    for error_type in subclasses:
        assert issubclass(error_type, TrackForgeError)


def test_error_fields() -> None:
    error = TrackForgeError(
        "something failed",
        retryable=True,
        user_action="try again",
        context={"field": "title"},
    )

    assert error.message == "something failed"
    assert error.retryable is True
    assert error.user_action == "try again"
    assert error.context == {"field": "title"}
    assert str(error) == "something failed"


def test_error_to_dict_is_json_serializable() -> None:
    error = RateLimited(
        "too many requests",
        context={
            "provider": "musicbrainz",
            "retry_after": 10,
            "details": {"host": "musicbrainz.org"},
        },
    )

    payload = error.to_dict()
    encoded = json.dumps(payload, sort_keys=True)

    assert payload["type"] == "RateLimited"
    assert payload["retryable"] is True
    assert "too many requests" in encoded
    assert json.loads(error.to_json())["context"]["provider"] == "musicbrainz"


def test_sensitive_context_is_redacted() -> None:
    error = TrackForgeError(
        "provider rejected request",
        context={
            "api_key": "super-secret",
            "authorization": "Bearer secret",
            "nested": {"password": "hunter2", "safe": "value"},
            "cookie_file": "/home/user/cookies.txt",
        },
    )

    assert error.context["api_key"] == "***"
    assert error.context["authorization"] == "***"
    assert error.context["nested"]["password"] == "***"  # noqa: S105
    assert error.context["nested"]["safe"] == "value"
    assert error.context["cookie_file"] == "***"
    assert "super-secret" not in error.to_json()
    assert "hunter2" not in error.to_json()


def test_retryable_defaults() -> None:
    assert ProviderUnavailable("down").retryable is True
    assert RateLimited("slow down").retryable is True
    assert RenderFailed("bad file").retryable is False


def test_validation_errors_are_value_errors() -> None:
    assert issubclass(DomainValidationError, ValueError)
    assert issubclass(InvalidId, DomainValidationError)
    assert issubclass(InvalidStateTransition, DomainValidationError)


def test_error_context_accepts_odd_values() -> None:
    error = TrackForgeError("bad context", context={"payload": b"secret", "path": object()})

    assert error.context["payload"] == "<binary:6 bytes>"
    assert isinstance(error.context["path"], str)


def test_error_context_non_mapping() -> None:
    error = TrackForgeError("bad context", context=b"bytes")  # type: ignore[arg-type]

    assert error.context["value"] == "<binary:5 bytes>"
