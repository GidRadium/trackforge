"""SSRF guard unit tests."""

from __future__ import annotations

import socket

import pytest

from trackforge.adapters.http.url_safety import (
    UrlSafetyGuard,
    is_safe_url,
    parse_http_url,
    redact_text,
    sanitize_url,
    validate_url,
)
from trackforge.domain.errors import UnsafeUrl

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "url",
    [
        "ftp://example.com/file",
        "file:///etc/passwd",
        "javascript:alert(1)",
        "http://127.0.0.1/audio",
        "http://10.0.0.1/audio",
        "http://192.168.1.2/audio",
        "http://169.254.169.254/latest/meta-data",
        "http://[::1]/audio",
        "http://localhost/audio",
        "http://example.local/audio",
    ],
)
def test_unsafe_urls_are_rejected(url: str) -> None:
    assert is_safe_url(url) is False
    with pytest.raises(UnsafeUrl):
        validate_url(url)


def test_public_https_url_is_accepted() -> None:
    parsed = parse_http_url("https://www.youtube.com/watch?v=demo")
    assert parsed.host == "www.youtube.com"
    assert parsed.port == 443
    assert is_safe_url("https://www.youtube.com/watch?v=demo") is True


def test_dns_private_result_is_blocked(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [(socket.AF_INET, 1, 6, "", ("10.1.2.3", 443))],
    )
    guard = UrlSafetyGuard()
    with pytest.raises(UnsafeUrl):
        guard.validate("https://not-local.example.com/audio")


def test_allow_private_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [(socket.AF_INET, 1, 6, "", ("10.1.2.3", 80))],
    )
    parsed = validate_url("http://internal.example.com/audio", allow_private=True)
    assert parsed.addresses == ("10.1.2.3",)


def test_redirect_chain_is_bounded() -> None:
    guard = UrlSafetyGuard(max_redirects=2, strict_dns=False)
    urls = ["https://www.youtube.com/watch?v=demo"] * 3
    assert len(guard.validate_redirect_chain(urls)) == 3
    with pytest.raises(UnsafeUrl):
        guard.validate_redirect_chain(urls + urls[:1])


def test_url_redaction_removes_query_userinfo_and_fragment() -> None:
    assert (
        sanitize_url("https://user:pass@example.com:8443/a/b?token=secret#frag")
        == "https://example.com:8443/a/b"
    )
    assert redact_text("failed at https://example.com/x?token=secret here") == (
        "failed at https://example.com/x here"
    )


def test_youtube_source_identity_is_preserved_while_tokens_are_removed() -> None:
    assert (
        sanitize_url("https://www.youtube.com/watch?v=abc_DEF-123&token=secret")
        == "https://www.youtube.com/watch?v=abc_DEF-123"
    )
    assert (
        sanitize_url("https://www.youtube.com/playlist?list=PLabc_-123&si=tracking")
        == "https://www.youtube.com/playlist?list=PLabc_-123"
    )
    assert (
        sanitize_url("https://youtu.be/abc_DEF-123?si=tracking") == "https://youtu.be/abc_DEF-123"
    )
