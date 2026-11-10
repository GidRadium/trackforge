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
    UnsupportedCollection,
    UnsupportedInput,
    UnsupportedScheme,
    UnsupportedUrl,
    VariantUnavailable,
)

ALL_ERRORS = [
    UnsupportedCollection,
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
        UnsupportedCollection,
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


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("ERROR: Unsupported URL: https://example.test/x", UnsupportedUrl),
        ("Sign in to confirm you're not a bot", AuthRequired),
        ("This video is private", AuthRequired),
        ("This video is available to Music Premium members", AuthRequired),
        ("This video is not available in your country", GeoBlocked),
        ("This video is DRM protected", DrmProtected),
        ("HTTP Error 429: Too Many Requests", RateLimited),
        ("Video unavailable", Unavailable),
        ("This video has been removed by the uploader", Unavailable),
        (
            (
                "This video is unavailable because the account associated with "
                "this video has been terminated"
            ),
            Unavailable,
        ),
        ("No video formats found!", NoFormats),
        ("Requested format is not available", NoFormats),
        ("Something unexpected broke", DownloadFailed),
        ("Unable to extract player response", DownloadFailed),
    ],
)
def test_ytdlp_errors_are_mapped(message: str, expected: type[AcquireError]) -> None:
    from yt_dlp.utils import DownloadError

    from trackforge.acquire.loaders.ytdlp_errors import map_ytdlp_error

    mapped = map_ytdlp_error(DownloadError(message), url="https://example.test/x")
    assert isinstance(mapped, expected)
    if isinstance(mapped, UnsupportedUrl):
        assert mapped.url == "https://example.test/x"


def test_extractor_error_is_mapped_too() -> None:
    from yt_dlp.utils import ExtractorError

    from trackforge.acquire.loaders.ytdlp_errors import map_ytdlp_error

    mapped = map_ytdlp_error(ExtractorError("This video is private"))
    assert isinstance(mapped, AuthRequired)


def test_error_messages_are_humanized_and_redacted() -> None:
    from yt_dlp.utils import DownloadError

    from trackforge.acquire.loaders.ytdlp_errors import humanize_error

    message = humanize_error(
        DownloadError("ERROR: failed via http://user:secret@proxy.test:8080")
    )
    assert message.startswith("failed via")
    assert "secret" not in message
    assert "<redacted>" in message


def test_retry_policy_comes_from_the_mapped_error() -> None:
    from yt_dlp.utils import DownloadError

    from trackforge.acquire.loaders.ytdlp_errors import is_retryable

    assert is_retryable(DownloadError("HTTP Error 429")) is True
    assert is_retryable(DownloadError("This video is private")) is False
    assert is_retryable(DownloadError("unknown failure")) is True


def test_error_categories_are_stable() -> None:
    from trackforge.acquire.loaders.ytdlp_errors import classify_ytdlp_error

    assert classify_ytdlp_error("DRM protected") == "drm"
    assert classify_ytdlp_error("not available in your country") == "geo"
    assert classify_ytdlp_error("Sign in") == "auth"
    assert classify_ytdlp_error("Too Many Requests") == "rate"
    assert classify_ytdlp_error("Unsupported URL") == "unsupported"
    assert classify_ytdlp_error("No video formats found") == "formats"
    assert classify_ytdlp_error("Video unavailable") == "unavailable"
    assert classify_ytdlp_error("something else") == "failed"


def test_error_message_does_not_leak_traceback_objects() -> None:
    import sys

    from yt_dlp.utils import DownloadError

    from trackforge.acquire.loaders.ytdlp_errors import humanize_error

    try:
        raise ValueError("inner")
    except ValueError:
        error = DownloadError(
            "ERROR: This video is unavailable",
            sys.exc_info(),  # pyright: ignore[reportArgumentType]
        )
    message = humanize_error(error)
    assert "traceback object" not in message
    assert "<class" not in message
    assert "This video is unavailable" in message
