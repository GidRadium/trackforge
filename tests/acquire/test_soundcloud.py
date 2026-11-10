"""SoundCloud loader, URL and recommendation tests (offline)."""

# pyright: standard

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from yt_dlp.utils import DownloadError

from trackforge.acquire.config import SourceProfile
from trackforge.acquire.errors import (
    AuthRequired,
    DownloadFailed,
    SearchNotSupported,
)
from trackforge.acquire.loaders.base import LoaderContext
from trackforge.acquire.loaders.soundcloud import SoundCloudLoader
from trackforge.acquire.loaders.ytdlp import YtDlpLoader
from trackforge.acquire.loaders.ytdlp_errors import map_ytdlp_error
from trackforge.acquire.models import (
    MediaKind,
    ResolutionState,
    SourceKind,
    SourceRef,
    UrlHint,
)
from trackforge.acquire.registry import default_registry

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def context() -> LoaderContext:
    return LoaderContext(
        job_id="job-soundcloud",
        profile=SourceProfile(name="soundcloud"),
    )


def inspect_fixture(
    monkeypatch: pytest.MonkeyPatch,
    info: dict[str, Any],
    *,
    flat: bool = False,
    limit: int | None = None,
):
    loader = SoundCloudLoader()

    def fake_extract(
        url: str,
        *,
        ctx: LoaderContext,
        flat: bool = False,
        limit: int | None = None,
    ) -> dict[str, Any]:
        del url, ctx, flat, limit
        return info

    monkeypatch.setattr(loader, "_extract", fake_extract)
    ref = SourceRef(
        "soundcloud",
        str(info["webpage_url"]),
        str(info["id"]),
    )
    return loader.get_info(ref, ctx=context(), flat=flat, limit=limit)


@pytest.mark.parametrize(
    "url",
    [
        "https://soundcloud.com/fixture-artist/neon-rain",
        "https://www.soundcloud.com/fixture-artist/neon-rain?si=abc",
        "https://m.soundcloud.com/fixture-artist/neon-rain",
    ],
)
def test_track_urls_are_single(url: str) -> None:
    loader = SoundCloudLoader()
    assert loader.supports_url(url) is True
    assert loader.classify_url(url) is UrlHint.SINGLE


@pytest.mark.parametrize(
    "url",
    [
        "https://soundcloud.com/fixture-artist/sets/fixture-set",
        "https://www.soundcloud.com/fixture-artist/sets/fixture-set?si=abc",
        "https://m.soundcloud.com/fixture-artist/sets/fixture-set",
    ],
)
def test_set_urls_are_collections(url: str) -> None:
    loader = SoundCloudLoader()
    assert loader.supports_url(url) is True
    assert loader.classify_url(url) is UrlHint.COLLECTION


@pytest.mark.parametrize(
    "url",
    [
        "https://soundcloud.com/fixture-artist",
        "https://soundcloud.com/discover",
        "https://soundcloud.com/search?q=rain",
        "https://soundcloud.com/fixture-artist/sets",
        "https://on.soundcloud.com/abc123",
        "https://soundcloud.com.evil.test/fixture-artist/neon-rain",
        "https://example.test/fixture-artist/neon-rain",
        "ftp://soundcloud.com/fixture-artist/neon-rain",
    ],
)
def test_non_media_or_foreign_urls_are_unknown(url: str) -> None:
    loader = SoundCloudLoader()
    assert loader.classify_url(url) is UrlHint.UNKNOWN


def test_rejects_foreign_hosts() -> None:
    loader = SoundCloudLoader()
    assert loader.supports_url(
        "https://soundcloud.com.evil.test/fixture-artist/neon-rain"
    ) is False
    assert loader.supports_url("https://example.test/x") is False


def test_track_maps_to_single_audio(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    info = load_fixture("soundcloud_track.info.json")
    source = inspect_fixture(monkeypatch, info)

    assert source.kind is SourceKind.SINGLE
    assert source.title == "Neon Rain"
    assert source.creator == "SoundCloud Fixture Artist"
    item = source.items[0]
    assert item.kind is MediaKind.AUDIO
    assert item.resolution is ResolutionState.FULL
    assert item.duration == pytest.approx(210.0)
    assert item.variants


def test_track_recommends_direct_original_audio(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    info = load_fixture("soundcloud_track.info.json")
    item = inspect_fixture(monkeypatch, info).items[0]

    selected = next(
        variant
        for variant in item.variants
        if variant.id == item.recommendations.audio
    )
    audio = next(stream for stream in selected.streams if stream.role.value == "audio")
    # The direct 128k HTTP rendition wins over a higher-bitrate HLS transcode.
    assert audio.protocol == "https"
    assert audio.format_id == "http_mp3_128"


def test_set_maps_to_flat_collection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    info = load_fixture("soundcloud_set.info.json")
    source = inspect_fixture(monkeypatch, info, flat=True, limit=1)

    assert source.kind is SourceKind.COLLECTION
    assert source.title == "Fixture Set"
    assert len(source.items) == 1
    item = source.items[0]
    assert item.resolution is ResolutionState.FLAT
    assert item.variants == ()
    assert item.page_url == "https://soundcloud.com/fixture-artist/set-track-one"
    assert item.position == 1


def test_cookies_are_optional() -> None:
    loader = SoundCloudLoader()
    assert loader.is_ready(SourceProfile(name="soundcloud")) is True


def test_search_is_deferred_to_l3() -> None:
    loader = SoundCloudLoader()
    with pytest.raises(SearchNotSupported):
        loader.search("rain", limit=5, ctx=context())


def test_403_maps_to_auth_required() -> None:
    mapped = map_ytdlp_error(
        DownloadError("ERROR: HTTP Error 403: Forbidden"),
        url="https://soundcloud.com/fixture-artist/neon-rain",
    )
    assert isinstance(mapped, AuthRequired)


def test_timeout_maps_to_retryable_download_failure() -> None:
    mapped = map_ytdlp_error(
        DownloadError("ERROR: Read timed out"),
        url="https://soundcloud.com/fixture-artist/neon-rain",
    )
    assert isinstance(mapped, DownloadFailed)
    assert mapped.retryable is True


def test_priority_and_registry() -> None:
    loader = SoundCloudLoader()
    assert loader.name == "soundcloud"
    assert loader.config_key == "soundcloud"
    assert loader.priority == 20
    assert loader.priority < YtDlpLoader.priority
    registry = default_registry()
    assert isinstance(registry.by_name("soundcloud"), SoundCloudLoader)
    resolved = registry.resolve("https://soundcloud.com/fixture-artist/neon-rain")
    assert isinstance(resolved.loader, SoundCloudLoader)
    assert resolved.profile.name == "soundcloud"
