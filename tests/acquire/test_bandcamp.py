"""Bandcamp track/album mapping through the generic yt-dlp loader."""

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
from trackforge.metadata.model import PartialDate

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def map_fixture(info: dict[str, Any], *, flat: bool = False):
    return YtDlpMapper(SourceProfile(name="yt_dlp")).map(
        info,
        SourceRef("yt-dlp", str(info["webpage_url"]), str(info["id"])),
        flat=flat,
    )


def test_bandcamp_track_maps_to_single_audio() -> None:
    source = map_fixture(load_fixture("bandcamp_track.info.json"))
    item = source.items[0]

    assert source.kind is SourceKind.SINGLE
    assert source.title == "Ben Prunty - Lanius (Battle)"
    assert item.title == "Lanius (Battle)"
    assert source.creator == "Ben Prunty"
    assert source.thumbnail is not None
    assert source.thumbnail.url.startswith("https://f4.bcbits.com/")
    assert item.kind is MediaKind.AUDIO
    assert item.resolution is ResolutionState.FULL
    assert item.artists == ("Ben Prunty",)
    assert item.album == "FTL: Advanced Edition Soundtrack"
    assert item.album_artists == ("Ben Prunty",)
    assert item.duration == pytest.approx(260.877)
    assert item.release_date == PartialDate(2014, 4, 3)
    assert item.extras["track_number"] == 1
    assert item.extras["track_id"] == "2650410135"
    assert item.extras["tags"]


def test_bandcamp_track_variants_and_recommendation() -> None:
    item = map_fixture(load_fixture("bandcamp_track.info.json")).items[0]
    variants = {variant.id: variant for variant in item.variants}

    assert all(variant.kind is MediaKind.AUDIO for variant in item.variants)
    assert "audio-mp3-128" in variants
    assert "audio-flac" in variants
    assert item.recommendations.audio in variants
    selected = variants[item.recommendations.audio or ""]
    assert all(stream.role is StreamRole.AUDIO for stream in selected.streams)


def test_bandcamp_album_maps_to_resolved_collection() -> None:
    source = map_fixture(load_fixture("bandcamp_album.info.json"))

    assert source.kind is SourceKind.COLLECTION
    assert source.title == "Minecraft - Volume Alpha"
    assert source.creator is None
    assert source.description
    assert len(source.items) == 3
    assert [item.position for item in source.items] == [1, 2, 3]
    assert [item.title for item in source.items] == ["Key", "Door", "Subwoofer Lullaby"]
    first = source.items[0]
    assert first.id == "1660675500"
    assert first.kind is MediaKind.AUDIO
    assert first.resolution is ResolutionState.FULL
    assert first.album == "Minecraft - Volume Alpha"
    assert first.artists == ("C418",)
    assert first.page_url == "https://c418.bandcamp.com/track/key"
    assert first.variants
    assert first.extras["track_number"] == 1
    assert first.release_date == PartialDate(2011, 3, 4)


def test_flat_bandcamp_album_has_no_variants() -> None:
    source = map_fixture(load_fixture("bandcamp_album.info.json"), flat=True)

    assert source.kind is SourceKind.COLLECTION
    assert all(item.resolution is ResolutionState.FLAT for item in source.items)
    assert all(item.variants == () for item in source.items)


@pytest.mark.parametrize(
    "url",
    [
        "https://benprunty.bandcamp.com/track/lanius-battle",
        "https://c418.bandcamp.com/album/minecraft-volume-alpha",
    ],
)
def test_bandcamp_resolves_through_generic_loader(url: str) -> None:
    loader = YtDlpLoader()
    assert loader.supports_url(url) is True
    assert loader.is_ready(SourceProfile(name="yt_dlp")) is True

    resolved = default_registry().resolve(url)
    assert resolved.loader.name == "yt-dlp"
    assert resolved.profile.name == "yt_dlp"
