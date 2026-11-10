"""L1.0 config/auth tests: env substitution, proxy and cookie guards."""

# pyright: standard

from __future__ import annotations

import os
from pathlib import Path

import pytest

from trackforge.acquire.auth import (
    ensure_auth_ready,
    inspect_cookie_file,
    resolve_proxy,
    validate_cookie_file,
)
from trackforge.acquire.config import SourceProfile, load_config
from trackforge.acquire.errors import AuthRequired, ConfigError, CookieError
from trackforge.acquire.redaction import redact_proxy, redact_text


def cookie_text(*, domain: str = ".youtube.com", name: str = "SID") -> str:
    return (
        "# Netscape HTTP Cookie File\n"
        f"{domain}\tTRUE\t/\tTRUE\t2147483647\t{name}\tSECRET-COOKIE-VALUE\n"
    )


def write_cookie_file(path: Path, mode: int = 0o600) -> Path:
    path.write_text(cookie_text(), encoding="utf-8")
    os.chmod(path, mode)
    return path


def test_source_profile_reads_cookies_and_proxy_from_config(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        """
[sources.default]
socket_timeout = 25
retries = 4

[sources.youtube]
cookies = "secrets/youtube.txt"
proxy = "${YOUTUBE_PROXY}"
impersonate = "chrome"

[sources.yandex_music]
cookies_from_browser = ["chrome", "default"]

[sources.soundcloud]
proxy = "none"
""",
        encoding="utf-8",
    )
    config = load_config(
        path,
        env={
            "HOME": str(tmp_path),
            "YOUTUBE_PROXY": "http://proxy.test:8080",
        },
        use_user_config=False,
    )

    youtube = config.profile_for("youtube")
    assert youtube.cookies_file == tmp_path / "secrets" / "youtube.txt"
    assert youtube.proxy == "http://proxy.test:8080"
    assert youtube.impersonate == "chrome"
    assert youtube.socket_timeout == 25.0
    assert youtube.retries == 4

    yandex = config.profile_for("yandex_music")
    assert yandex.cookies_from_browser == ("chrome", "default")
    assert yandex.cookies_file is None
    assert config.profile_for("soundcloud").proxy == "none"


def test_missing_env_variable_is_a_config_error(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        '[sources.youtube]\nproxy = "${L1_0_MISSING_PROXY}"\n',
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="L1_0_MISSING_PROXY"):
        load_config(
            path,
            env={"HOME": str(tmp_path)},
            use_user_config=False,
        )


def test_proxy_none_env_and_url_resolution() -> None:
    assert resolve_proxy(None) is None
    assert resolve_proxy("") is None
    assert resolve_proxy("none") is None
    assert resolve_proxy("NONE") is None
    assert resolve_proxy("env", env={}) is None
    assert (
        resolve_proxy("env", env={"HTTPS_PROXY": "http://proxy.test:8080"})
        == "http://proxy.test:8080"
    )
    assert (
        resolve_proxy("env", env={"HTTP_PROXY": "socks5://proxy.test:1080"})
        == "socks5://proxy.test:1080"
    )
    assert (
        resolve_proxy("http://user:pass@proxy.test:8080")
        == "http://user:pass@proxy.test:8080"
    )


def test_invalid_proxy_error_does_not_leak_credentials() -> None:
    with pytest.raises(ConfigError) as error:
        resolve_proxy("http://user:supersecret@")
    message = str(error.value)
    assert "supersecret" not in message
    assert "user" not in message
    assert "<invalid>" in message or "no host" in message


def test_redaction_removes_proxy_credentials_and_tokens() -> None:
    assert redact_proxy("http://user:secret@proxy.test:8080") == "http://proxy.test:8080"
    assert redact_proxy("socks5://user:secret@proxy.test") == "socks5://proxy.test"
    assert redact_proxy("env") == "env"
    assert redact_proxy("none") == "none"

    text = (
        "GET http://user:secret@proxy.test?token=abc "
        "Cookie: SID=SECRET Authorization: Bearer TOKEN"
    )
    cleaned = redact_text(text, ["TOKEN"])
    for secret in ("secret", "SECRET", "abc", "TOKEN", "Bearer"):
        assert secret not in cleaned
    assert "<redacted>" in cleaned


def test_cookie_file_inspection_and_private_modes(tmp_path: Path) -> None:
    private = write_cookie_file(tmp_path / "private.txt", 0o600)
    public = write_cookie_file(tmp_path / "public.txt", 0o644)

    private_result = inspect_cookie_file(private)
    assert private_result.ok
    assert private_result.private
    assert private_result.has_header
    assert private_result.data_lines == 1
    assert "SECRET" not in repr(private_result)

    public_result = inspect_cookie_file(public)
    assert public_result.format_ok
    assert public_result.world_readable
    assert not public_result.private

    with pytest.raises(CookieError, match="world-readable"):
        validate_cookie_file(public, require_private=True)


def test_invalid_cookie_format_is_a_content_free_error(tmp_path: Path) -> None:
    path = tmp_path / "bad.txt"
    path.write_text(
        "# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\n",
        encoding="utf-8",
    )
    os.chmod(path, 0o600)
    with pytest.raises(CookieError) as error:
        validate_cookie_file(path)
    assert "SECRET" not in str(error.value)
    assert "7 tab-separated" in str(error.value)


def test_headerless_cookie_file_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "headerless.txt"
    path.write_text(
        ".youtube.com\tTRUE\t/\tTRUE\t2147483647\tSID\tSECRET-COOKIE-VALUE\n",
        encoding="utf-8",
    )
    os.chmod(path, 0o600)
    result = inspect_cookie_file(path)
    assert not result.format_ok
    assert "header" in (result.error or "")
    with pytest.raises(CookieError, match="Netscape"):
        validate_cookie_file(path)


def test_auth_guard_requires_cookies_for_youtube(tmp_path: Path) -> None:
    with pytest.raises(AuthRequired, match="requires cookies"):
        ensure_auth_ready(
            SourceProfile(name="youtube"),
            source="youtube",
            cookies_required=True,
        )

    file_profile = SourceProfile(
        name="youtube",
        cookies_file=write_cookie_file(tmp_path / "cookies.txt"),
    )
    ensure_auth_ready(file_profile, source="youtube", cookies_required=True)

    browser_profile = SourceProfile(
        name="youtube",
        cookies_from_browser=("chrome",),
    )
    ensure_auth_ready(browser_profile, source="youtube", cookies_required=True)


def test_auth_guard_rejects_missing_and_public_cookie_files(tmp_path: Path) -> None:
    with pytest.raises(CookieError, match="not found"):
        ensure_auth_ready(
            SourceProfile(name="youtube", cookies_file=tmp_path / "missing.txt"),
            source="youtube",
            cookies_required=True,
        )

    public = write_cookie_file(tmp_path / "public.txt", 0o644)
    with pytest.raises(CookieError, match="world-readable"):
        ensure_auth_ready(
            SourceProfile(name="youtube", cookies_file=public),
            source="youtube",
            cookies_required=True,
        )
