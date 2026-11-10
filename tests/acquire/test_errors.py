"""Tests for typed acquisition errors."""

# pyright: standard

from __future__ import annotations

import pytest

from trackforge.acquire.errors import (
    AcquireError,
    AmbiguousInput,
    AuthRequired,
    CancelledError,
    ConfigError,
    CookieError,
    DownloadFailed,
    DrmProtected,
    GeoBlocked,
    NoFormats,
    PostprocessError,
    ProxyError,
    RateLimited,
    SearchNotSupported,
    SearchUnavailable,
    Unavailable,
    UnsupportedInput,
    UnsupportedScheme,
    UnsupportedUrl,
    VariantUnavailable,
)

ALL_ERRORS = [
    UnsupportedInput,
    UnsupportedScheme,
    UnsupportedUrl,
    AmbiguousInput,
    SearchUnavailable,
    AuthRequired,
    CookieError,
    ProxyError,
    GeoBlocked,
    DrmProtected,
    RateLimited,
    Unavailable,
    NoFormats,
    VariantUnavailable,
    DownloadFailed,
    PostprocessError,
    CancelledError,
    ConfigError,
    SearchNotSupported,
]


@pytest.mark.parametrize("error_type", ALL_ERRORS)
def test_every_error_is_an_acquire_error(error_type: type[AcquireError]) -> None:
    assert issubclass(error_type, AcquireError)
    assert isinstance(error_type.retryable, bool)


@pytest.mark.parametrize(
    "error_type",
    [
        UnsupportedInput,
        UnsupportedScheme,
        AmbiguousInput,
        AuthRequired,
        CookieError,
        GeoBlocked,
        DrmProtected,
        Unavailable,
        NoFormats,
        VariantUnavailable,
        PostprocessError,
        CancelledError,
        ConfigError,
    ],
)
def test_non_retryable_errors(error_type: type[AcquireError]) -> None:
    assert error_type.retryable is False


@pytest.mark.parametrize(
    "error_type",
    [SearchUnavailable, ProxyError, RateLimited, DownloadFailed],
)
def test_retryable_errors(error_type: type[AcquireError]) -> None:
    assert error_type.retryable is True


def test_hints_are_present_for_actionable_errors() -> None:
    assert AuthRequired.hint
    assert CookieError.hint
    assert ProxyError.hint
    assert GeoBlocked.hint
    assert DrmProtected.hint
    assert RateLimited.hint
    assert Unavailable.hint
    assert NoFormats.hint
    assert VariantUnavailable.hint
    assert UnsupportedUrl.hint


def test_message_default_and_custom_hint() -> None:
    error = DownloadFailed()
    assert str(error) == "download failed"
    assert error.retryable is True

    error = ConfigError("bad config", hint="fix it")
    assert str(error) == "bad config"
    assert error.hint == "fix it"


def test_unsupported_url_keeps_url_and_source() -> None:
    error = UnsupportedUrl("https://example.test/x", source="youtube")
    assert error.url == "https://example.test/x"
    assert error.source == "youtube"
    assert "youtube" in str(error)


def test_search_not_supported_names_loader() -> None:
    error = SearchNotSupported("fake")
    assert error.source == "fake"
    assert "fake" in str(error)
    assert error.retryable is False
