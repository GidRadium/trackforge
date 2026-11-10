"""Vimeo policy and offline mapping tests.

Vimeo is served by the generic yt-dlp loader.  A light site-specific class is
not needed at L1.4, but the generic loader must still pair Vimeo's separate
audio and video HLS renditions correctly.
"""

# pyright: standard

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from trackforge.acquire.config import SourceProfile
from trackforge.acquire.loaders.ytdlp import YtDlpLoader
from trackforge.acquire.loaders.ytdlp_mapper import YtDlpMapper
from trackforge.acquire.models import (
    MediaKind,
    ResolutionState,
    SourceKind,
    SourceRef,
    StreamRole,
)
from trackforge.acquire.registry import default_registry

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def map_fixture(info: dict[str, Any], *, flat: bool = False):
    return YtDlpMapper(SourceProfile(name="yt_dlp")).map(
        info,
        SourceRef("yt-dlp", str(info["webpage_url"]), str(info["id"])),
        flat=flat,
    )


def test_vimeo_video_maps_to_single() -> None:
    info = load_fixture("vimeo_video.info.json")
    source = map_fixture(info)
    item = source.items[0]

    assert source.kind is SourceKind.SINGLE
    assert source.title == "The New Vimeo Player (You Know, For Videos)"
    assert source.creator == "Vimeo"
    assert source.description is None
    assert source.thumbnail is not None
    assert source.thumbnail.url.startswith("https://i.vimeocdn.com/")
    assert item.id == "76979871"
    assert item.title == source.title
    assert item.duration == pytest.approx(62.0)
    assert item.page_url == "https://player.vimeo.com/video/76979871"
    assert item.kind is MediaKind.AUDIO_VIDEO
    assert item.resolution is ResolutionState.FULL
    # Vimeo's player payload does not expose upload/release dates.
    assert item.release_date is None


def test_vimeo_video_pairs_separate_hls_audio_and_video() -> None:
    item = map_fixture(load_fixture("vimeo_video.info.json")).items[0]
    variants = {variant.id: variant for variant in item.variants}

    assert "720p-mp4" in variants
    assert "audio-mp4" in variants
    selected = variants["720p-mp4"]
    roles = {stream.role for stream in selected.streams}
    assert roles == {StreamRole.VIDEO, StreamRole.AUDIO}
    assert selected.requires_merge is True
    assert item.recommendations.video == "720p-mp4"
    assert item.recommendations.audio == "audio-mp4"
    assert all(variant.streams for variant in item.variants)


def test_vimeo_audio_only_hls_is_not_a_progressive_video() -> None:
    item = map_fixture(load_fixture("vimeo_video.info.json")).items[0]
    audio_variants = [
        variant for variant in item.variants if variant.kind is MediaKind.AUDIO
    ]

    assert len(audio_variants) == 1
    audio = audio_variants[0]
    assert audio.id == "audio-mp4"
    assert audio.streams[0].role is StreamRole.AUDIO
    assert audio.streams[0].height is None


def test_vimeo_album_maps_to_flat_collection() -> None:
    info = load_fixture("vimeo_playlist.info.json")
    source = map_fixture(info, flat=True)

    assert source.kind is SourceKind.COLLECTION
    assert source.title == "Staff Favorites: November 2013"
    assert len(source.items) == 5
    assert [item.position for item in source.items] == [1, 2, 3, 4, 5]
    assert [item.id for item in source.items[:2]] == ["79761619", "79695097"]
    assert source.items[0].page_url == "https://vimeo.com/79761619"
    assert all(item.resolution is ResolutionState.FLAT for item in source.items)
    assert all(item.variants == () for item in source.items)


def test_flat_vimeo_entry_does_not_become_a_direct_variant() -> None:
    """A flat entry's page URL must not be treated as a media URL."""
    source = map_fixture(load_fixture("vimeo_playlist.info.json"), flat=False)

    assert all(item.variants == () for item in source.items)
    assert all(item.kind is MediaKind.UNKNOWN for item in source.items)


@pytest.mark.parametrize(
    "url",
    [
        "https://vimeo.com/76979871",
        "https://www.vimeo.com/76979871",
        "https://player.vimeo.com/video/76979871",
        "https://vimeo.com/album/2632481",
    ],
)
def test_generic_loader_accepts_vimeo_urls(url: str) -> None:
    loader = YtDlpLoader()
    assert loader.supports_url(url) is True
    assert loader.is_ready(SourceProfile(name="yt_dlp")) is True


def test_vimeo_resolves_through_generic_loader() -> None:
    registry = default_registry()
    resolved = registry.resolve("https://vimeo.com/76979871")

    assert resolved.loader.name == "yt-dlp"
    assert resolved.profile.name == "yt_dlp"
