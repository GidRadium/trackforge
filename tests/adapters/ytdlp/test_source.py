"""YtDlpSource resolution, format selection, cookies, and metadata tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from tests.adapters.ytdlp.conftest import (
    SOUNDCLOUD_URL,
    YOUTUBE_URL,
    load_fixture,
    read_log,
)

from trackforge.adapters.sources.ytdlp.client import YtDlpClient
from trackforge.adapters.sources.ytdlp.metadata import (
    build_source_metadata,
    sanitize_snapshot,
)
from trackforge.adapters.sources.ytdlp.source import YtDlpSource, select_best_audio
from trackforge.domain.errors import (
    CookiesPermissionsError,
    DrmProtected,
    OfflineModeError,
    Unavailable,
    UnsafeUrl,
    UnsupportedUrl,
)
from trackforge.domain.model.source import Format, Url

pytestmark = pytest.mark.unit


def test_supports_only_safe_http_urls(fake_client: YtDlpClient) -> None:
    source = YtDlpSource(client=fake_client)
    assert source.supports(Url(YOUTUBE_URL)) is True
    assert source.supports(Url("ftp://example.com/a")) is False
    assert source.supports(Url("http://127.0.0.1/audio")) is False
    assert source.supports(Url("http://localhost/audio")) is False


@pytest.mark.asyncio
async def test_resolve_youtube_maps_audio_formats_only(
    fake_client: YtDlpClient,
) -> None:
    source = YtDlpSource(client=fake_client)
    resolved = await source.resolve(Url(YOUTUBE_URL))
    assert len(resolved) == 1
    candidate = resolved.candidates[0]
    assert candidate.external_id == "fixture-yt-1"
    assert candidate.uploader == "Fixture Uploader - Topic"
    assert candidate.duration_seconds == 2.0
    assert [fmt.format_id for fmt in candidate.formats] == ["140", "251"]
    assert all(fmt.container != "mp4" for fmt in candidate.formats)
    assert candidate.extra["selected_format_id"] == "140"
    assert candidate.extra["extractor"] == "youtube"
    assert candidate.extra["webpage_url"] == "https://www.youtube.com/watch?v=demo"
    assert len(candidate.thumbnails) == 2


@pytest.mark.asyncio
async def test_resolve_soundcloud(fake_client: YtDlpClient) -> None:
    source = YtDlpSource(client=fake_client)
    resolved = await source.resolve(Url(SOUNDCLOUD_URL))
    candidate = resolved.candidates[0]
    assert candidate.external_id == "fixture-sc-1"
    assert candidate.formats[0].container == "mp3"
    assert candidate.extra["selected_format_id"] == "hls_mp3_128"


def test_select_best_audio_prefers_m4a_then_bitrate() -> None:
    formats = [
        Format(format_id="251", container="webm", codec="opus", bitrate=160_000),
        Format(format_id="140", container="m4a", codec="aac", bitrate=128_000),
        Format(
            format_id="drm",
            container="m4a",
            codec="aac",
            bitrate=999_000,
            drm=True,
        ),
    ]
    assert select_best_audio(formats).format_id == "140"
    assert select_best_audio(formats[:1]).format_id == "251"


@pytest.mark.asyncio
async def test_drm_is_rejected(fake_client: YtDlpClient) -> None:
    source = YtDlpSource(client=fake_client)
    with pytest.raises(DrmProtected):
        await source.resolve(Url("https://www.youtube.com/watch?v=drm"))


@pytest.mark.asyncio
async def test_playlist_is_rejected(fake_client: YtDlpClient, fake_log: Path) -> None:
    source = YtDlpSource(client=fake_client)
    with pytest.raises(UnsupportedUrl):
        await source.resolve(Url("https://www.youtube.com/playlist?list=demo"))
    with pytest.raises(UnsupportedUrl):
        await source.resolve(Url("https://www.youtube.com/watch?v=demo&list=demo"))
    assert read_log(fake_log) == []


@pytest.mark.asyncio
async def test_no_audio_formats_is_unavailable(fake_client: YtDlpClient) -> None:
    source = YtDlpSource(client=fake_client)
    with pytest.raises(Unavailable):
        await source.resolve(Url("https://www.youtube.com/watch?v=missing-formats"))


@pytest.mark.asyncio
async def test_offline_does_not_invoke_downloader(
    fake_client: YtDlpClient,
    fake_log: Path,
) -> None:
    source = YtDlpSource(client=fake_client)
    with pytest.raises(OfflineModeError):
        await source.resolve(Url(YOUTUBE_URL), {"offline": True})
    assert read_log(fake_log) == []


@pytest.mark.asyncio
async def test_private_dns_result_is_blocked(
    fake_client: YtDlpClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import socket

    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [(socket.AF_INET, 1, 6, "", ("10.0.0.5", 443))],
    )
    source = YtDlpSource(client=fake_client)
    with pytest.raises(UnsafeUrl):
        await source.resolve(Url("https://www.youtube.com/watch?v=demo"))


@pytest.mark.asyncio
async def test_cookie_selected_by_domain_and_not_logged(
    fake_client: YtDlpClient,
    fake_log: Path,
    tmp_path: Path,
) -> None:
    cookie_dir = tmp_path / "cookies"
    cookie_dir.mkdir()
    cookie = cookie_dir / "youtube.com.txt"
    secret = "super-secret-cookie-value"  # noqa: S105
    cookie.write_text(
        f"# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t2147483647\tSID\t{secret}\n",
        encoding="utf-8",
    )
    cookie.chmod(0o600)
    source = YtDlpSource(client=fake_client, cookie_dir=cookie_dir)
    await source.resolve(Url(YOUTUBE_URL))
    args = read_log(fake_log)[-1]
    assert "--cookies" in args
    assert str(cookie.resolve()) in args
    assert secret not in json.dumps(args)


@pytest.mark.asyncio
async def test_cookie_is_not_sent_to_another_site(
    fake_client: YtDlpClient,
    fake_log: Path,
    tmp_path: Path,
) -> None:
    cookie_dir = tmp_path / "cookies"
    cookie_dir.mkdir()
    cookie = cookie_dir / "youtube.com.txt"
    cookie.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
    cookie.chmod(0o600)
    source = YtDlpSource(client=fake_client, cookie_dir=cookie_dir)
    await source.resolve(Url(SOUNDCLOUD_URL))
    assert "--cookies" not in read_log(fake_log)[-1]


@pytest.mark.asyncio
async def test_cookie_with_wrong_mode_is_not_used(
    fake_client: YtDlpClient,
    tmp_path: Path,
) -> None:
    cookie_dir = tmp_path / "cookies"
    cookie_dir.mkdir()
    cookie = cookie_dir / "youtube.com.txt"
    cookie.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
    cookie.chmod(0o644)
    source = YtDlpSource(client=fake_client, cookie_dir=cookie_dir)
    with pytest.raises(CookiesPermissionsError):
        await source.resolve(Url(YOUTUBE_URL))


def test_source_metadata_is_normalized_and_cookie_keys_removed() -> None:
    raw = load_fixture("single_youtube.json")
    raw["cookies"] = "should-never-be-stored"
    raw["http_headers"] = {"Cookie": "session=secret", "User-Agent": "fixture"}

    sanitized = sanitize_snapshot(raw)
    encoded = json.dumps(sanitized)
    assert "cookies" not in sanitized
    assert "should-never-be-stored" not in encoded
    assert "session=secret" not in encoded
    assert sanitized["http_headers"] == {"User-Agent": "fixture"}

    metadata = build_source_metadata(sanitized)
    assert metadata.external_id == "fixture-yt-1"
    assert metadata.title.startswith("Fixture YouTube Song")
    assert metadata.uploader == "Fixture Uploader - Topic"
    assert metadata.upload_date == "20240102"
    assert metadata.tags == ["fixture", "electronic"]
    assert metadata.duration_seconds == 2.0
    assert metadata.thumbnails[0].width == 480
    assert metadata.chapters[0].title == "Fixture chapter"
    assert metadata.webpage_url == "https://www.youtube.com/watch?v=demo"


@pytest.mark.asyncio
async def test_yandex_single_track_url_is_not_treated_as_playlist(
    fake_client: YtDlpClient,
    fake_log: Path,
) -> None:
    source = YtDlpSource(client=fake_client)

    resolved = await source.resolve(Url("https://music.yandex.ru/album/1/track/2"))

    assert resolved.candidates
    assert read_log(fake_log)


@pytest.mark.asyncio
async def test_configured_cookie_file_is_used_for_matching_host(
    tmp_path: Path,
    fake_client: YtDlpClient,
    fake_log: Path,
) -> None:
    cookie = tmp_path / "account-cookies.txt"
    cookie.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
    cookie.chmod(0o600)
    source = YtDlpSource(
        client=fake_client,
        cookie_dir=tmp_path / "cookies",
        cookie_files={"youtube.com": str(cookie)},
    )

    await source.resolve(Url(YOUTUBE_URL))

    args = read_log(fake_log)[-1]
    assert args[args.index("--cookies") + 1] == str(cookie.resolve())
