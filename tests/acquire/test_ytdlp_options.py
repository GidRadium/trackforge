"""L1.0 tests for captured ``YoutubeDL`` options."""

# pyright: standard

from __future__ import annotations

import logging
import os
from pathlib import Path

import pytest

from trackforge.acquire.config import SourceProfile
from trackforge.acquire.errors import AuthRequired
from trackforge.acquire.loaders.base import LoaderContext
from trackforge.acquire.loaders.ytdlp import YtDlpLoader
from trackforge.acquire.loaders.ytdlp_options import (
    build_ydl_options,
    create_youtube_dl,
    prepare_ydl_options,
)
from trackforge.acquire.models import SourceRef
from trackforge.acquire.staging import StagingArea


def write_cookie_file(path: Path, mode: int = 0o600) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "# Netscape HTTP Cookie File\n"
        ".youtube.com\tTRUE\t/\tTRUE\t2147483647\tSID\tCOOKIE-SECRET\n",
        encoding="utf-8",
    )
    os.chmod(path, mode)
    return path


def profile(**kwargs) -> SourceProfile:
    return SourceProfile(name="youtube", **kwargs)


def test_options_contain_cookiefile_proxy_and_js_runtime(tmp_path: Path) -> None:
    source = write_cookie_file(tmp_path / "config" / "youtube.txt")
    options = build_ydl_options(
        profile(
            cookies_file=source,
            proxy="http://proxy.test:8080",
            socket_timeout=12,
            retries=2,
            extractor_args={"youtube": {"player_client": ("web", "tv")}},
            headers={"Accept-Language": "ru"},
            user_agent="TrackForge/1.0",
        ),
    )

    assert options["cookiefile"] == str(source)
    assert options["proxy"] == "http://proxy.test:8080"
    assert options["socket_timeout"] == 12
    assert options["retries"] == 2
    assert options["fragment_retries"] == 2
    assert options["extractor_retries"] == 2
    assert options["extractor_args"] == {"youtube": {"player_client": ["web", "tv"]}}
    assert options["http_headers"] == {
        "Accept-Language": "ru",
        "User-Agent": "TrackForge/1.0",
    }
    assert options["js_runtimes"]["deno"]["path"].endswith("deno")
    assert options["cachedir"] is False
    assert options["remote_components"] == []


def test_cookies_from_browser_is_a_tuple(tmp_path: Path) -> None:
    options = build_ydl_options(
        profile(cookies_from_browser=("chrome", "Profile 1")),
    )
    assert options["cookiesfrombrowser"] == ("chrome", "Profile 1")
    assert "cookiefile" not in options


def test_proxy_none_is_absent_from_options(tmp_path: Path) -> None:
    options = build_ydl_options(profile(proxy="none"))
    assert "proxy" not in options


def test_proxy_env_is_resolved_from_environment(tmp_path: Path) -> None:
    options = build_ydl_options(
        profile(proxy="env"),
        env={
            "HTTP_PROXY": "http://http-proxy.test:8080",
            "HTTPS_PROXY": "http://https-proxy.test:8443",
        },
    )
    assert options["proxy"] == "http://https-proxy.test:8443"

    fallback = build_ydl_options(
        profile(proxy="env"),
        env={"ALL_PROXY": "socks5://all-proxy.test:1080"},
    )
    assert fallback["proxy"] == "socks5://all-proxy.test:1080"

    direct = build_ydl_options(profile(proxy="env"), env={})
    assert "proxy" not in direct


def test_proxy_credentials_are_redacted_in_loader_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    options = build_ydl_options(
        profile(proxy="http://user:supersecret@proxy.test:8080"),
    )
    logger = options["logger"]
    with caplog.at_level(logging.WARNING):
        logger.warning(
            "request via http://user:supersecret@proxy.test:8080 failed"
        )
    rendered = caplog.text
    assert "supersecret" not in rendered
    assert "user:supersecret" not in rendered
    assert "<redacted>" in rendered


def test_prepare_options_uses_configured_cookies_directly(tmp_path: Path) -> None:
    source = write_cookie_file(tmp_path / "config" / "youtube.txt", 0o600)
    staging = StagingArea(tmp_path / "staging")

    options = prepare_ydl_options(
        profile(cookies_file=source, proxy="none"),
        staging=staging,
        job_id="job-1",
    )

    assert options["cookiefile"] == str(source)
    assert not (staging.root / "job-1").exists()
    assert "proxy" not in options


def test_writable_original_cookies_are_passed_directly(tmp_path: Path) -> None:
    source = write_cookie_file(tmp_path / "original.txt", 0o600)
    options = build_ydl_options(profile(cookies_file=source))
    assert options["cookiefile"] == str(source)


def test_read_only_original_cookies_can_be_used_directly(tmp_path: Path) -> None:
    source = write_cookie_file(tmp_path / "original.txt", 0o400)
    options = build_ydl_options(profile(cookies_file=source))
    assert options["cookiefile"] == str(source)


def test_captured_youtube_dl_receives_cookie_and_proxy_options(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import yt_dlp

    source = write_cookie_file(tmp_path / "config" / "youtube.txt")
    options = build_ydl_options(
        profile(cookies_file=source, proxy="http://proxy.test:8080"),
    )
    captured: dict[str, object] = {}

    class CapturingYoutubeDL:
        def __init__(self, opts: dict[str, object]) -> None:
            captured.update(opts)

    monkeypatch.setattr(yt_dlp, "YoutubeDL", CapturingYoutubeDL)
    create_youtube_dl(options)

    assert captured["cookiefile"] == str(source)
    assert captured["proxy"] == "http://proxy.test:8080"


def test_loader_base_options_use_the_same_builder(tmp_path: Path) -> None:
    source = write_cookie_file(tmp_path / "config" / "youtube.txt")
    options = YtDlpLoader()._base_options(
        profile(cookies_file=source, proxy="none"),
    )
    assert options["cookiefile"] == str(source)
    assert "proxy" not in options


def test_youtube_profile_without_cookies_is_not_ready() -> None:
    loader = YtDlpLoader()
    assert loader.is_ready(SourceProfile(name="youtube")) is False
    assert (
        loader.is_ready(
            SourceProfile(name="youtube", cookies_from_browser=("chrome",))
        )
        is True
    )
    assert loader.is_ready(SourceProfile(name="yt_dlp")) is True


def test_prepare_options_requires_cookies_when_asked(tmp_path: Path) -> None:
    staging = StagingArea(tmp_path / "staging")
    with pytest.raises(AuthRequired, match="requires cookies"):
        prepare_ydl_options(
            SourceProfile(name="youtube"),
            staging=staging,
            job_id="job-1",
            cookies_required=True,
        )


def test_youtube_get_info_preflight_raises_auth_required_before_network() -> None:
    loader = YtDlpLoader()
    ctx = LoaderContext(
        job_id="job-1",
        profile=SourceProfile(name="yt_dlp"),
    )
    with pytest.raises(AuthRequired, match="requires cookies"):
        loader.get_info(
            SourceRef(
                source="yt-dlp",
                url="https://www.youtube.com/watch?v=test",
            ),
            ctx=ctx,
        )
