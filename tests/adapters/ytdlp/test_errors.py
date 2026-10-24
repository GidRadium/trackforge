"""Typed error mapping tests."""

from __future__ import annotations

import pytest

from trackforge.adapters.sources.ytdlp.errors import classify_error, is_retryable, map_error
from trackforge.domain.errors import (
    CookiesExpired,
    DrmProtected,
    ExtractorBroken,
    GeoBlocked,
    LoginRequired,
    PreviewOnly,
    ProviderUnavailable,
    RateLimited,
    Unavailable,
    UnsupportedUrl,
)

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("stderr", "expected"),
    [
        ("ERROR: Sign in to confirm you're not a bot", LoginRequired),
        ("ERROR: This video is age-restricted; login required", LoginRequired),
        ("ERROR: cookies are no longer valid; cookie expired", CookiesExpired),
        ("ERROR: HTTP Error 429: Too Many Requests", RateLimited),
        ("ERROR: This video is not available in your country", GeoBlocked),
        ("ERROR: DRM protected stream cannot be downloaded", DrmProtected),
        ("ERROR: Video unavailable", Unavailable),
        ("ERROR: Unsupported URL: ftp://example.com", UnsupportedUrl),
        ("ERROR: Unable to extract player response", ExtractorBroken),
        ("ERROR: Requested format is not available", ExtractorBroken),
        ("ERROR: HTTP Error 503: Service Unavailable", ProviderUnavailable),
        ("ERROR: smart_preview stream is only a preview", PreviewOnly),
    ],
)
def test_error_mapping(stderr: str, expected: type[Exception]) -> None:
    error = map_error(stderr, returncode=1, url="https://example.com/a?token=secret")
    assert isinstance(error, expected)
    serialized = str(error.to_dict())
    assert "secret" not in serialized


def test_login_and_drm_are_not_retryable() -> None:
    login = map_error("Sign in to confirm you're not a bot")
    drm = map_error("DRM protected")
    rate = map_error("HTTP Error 429")
    assert is_retryable(login) is False
    assert is_retryable(drm) is False
    assert is_retryable(rate) is True


def test_cookie_values_are_redacted() -> None:
    error = map_error("ERROR: cookie: session=super-secret-cookie-value")
    assert "super-secret-cookie-value" not in str(error.to_dict())
    assert classify_error("ERROR: cookie expired") is CookiesExpired


def test_requested_format_unavailable_points_at_js_solver() -> None:
    from trackforge.adapters.sources.ytdlp.errors import map_error

    error = map_error("ERROR: [youtube] x: Requested format is not available")

    assert isinstance(error, ExtractorBroken)
    assert "JavaScript" in error.message
    assert "yt-dlp[default]" in (error.user_action or "")
