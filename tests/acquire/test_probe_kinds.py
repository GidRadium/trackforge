"""Probe how Pinterest and VK payloads fit the current SourceInfo model.

These are intentionally *probes*, not full loaders.  They document the gaps
that L5 must close: image-only pins have thumbnails but no media variant, and
a VK wall is mapped as a collection rather than a post.
"""

# pyright: standard

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from trackforge.acquire.config import SourceProfile
from trackforge.acquire.loaders.ytdlp_mapper import YtDlpMapper
from trackforge.acquire.models import (
    MediaKind,
    ResolutionState,
    SourceKind,
    SourceRef,
)

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def map_fixture(info: dict[str, Any], *, flat: bool = False):
    return YtDlpMapper(SourceProfile(name="yt_dlp")).map(
        info,
        SourceRef("yt-dlp", str(info["webpage_url"]), str(info["id"])),
        flat=flat,
    )


def test_pinterest_image_pin_is_single_without_image_variant() -> None:
    source = map_fixture(load_fixture("pinterest_pin.info.json"))
    item = source.items[0]

    assert source.kind is SourceKind.SINGLE
    assert source.kind is not SourceKind.POST
    assert source.title == "Enamel Pin DIY"
    assert source.description
    assert source.thumbnail is not None
    assert source.thumbnail.url.endswith("originals/25/f7/b6/25f7b61a1d8b306cdf2dd59458697127.jpg")
    assert item.resolution is ResolutionState.FULL
    assert item.variants == ()
    assert item.kind is MediaKind.UNKNOWN
    # The image is only exposed as a thumbnail; there is no IMAGE variant yet.
    assert all(variant.kind is not MediaKind.IMAGE for variant in item.variants)
    assert item.thumbnail is not None
    assert item.thumbnail.width == 800
    assert item.thumbnail.height == 1600


def test_pinterest_video_pin_is_still_single_not_post() -> None:
    info = {
        "_type": "video",
        "id": "1084663891475263837",
        "title": "Pinterest video probe",
        "description": "A video pin, not an image pin.",
        "uploader": "Fixture Creator",
        "duration": 14.9,
        "webpage_url": "https://www.pinterest.com/pin/1084663891475263837/",
        "thumbnails": [
            {
                "url": "https://i.pinimg.com/736x/fixture.jpg",
                "width": 736,
                "height": 1308,
            }
        ],
        "formats": [
            {
                "format_id": "V_720P",
                "ext": "mp4",
                "vcodec": "avc1.4d401f",
                "acodec": "mp4a.40.2",
                "height": 720,
                "width": 1280,
                "protocol": "https",
                "url": "https://media.example.test/pinterest/video.mp4",
            }
        ],
    }
    source = map_fixture(info)

    assert source.kind is SourceKind.SINGLE
    assert source.items[0].kind is MediaKind.AUDIO_VIDEO
    assert source.items[0].variants


def test_vk_wall_is_mapped_as_collection_not_post() -> None:
    source = map_fixture(load_fixture("vk_wall.info.json"))

    assert source.kind is SourceKind.COLLECTION
    assert source.kind is not SourceKind.POST
    assert source.title and "Wall post" in source.title
    assert source.description
    assert len(source.items) == 2
    video, audio = source.items
    assert video.kind is MediaKind.AUDIO_VIDEO
    assert video.resolution is ResolutionState.FULL
    assert video.page_url == "https://vk.com/video-2000843141_119843141"
    assert video.variants
    assert audio.kind is MediaKind.AUDIO
    assert audio.resolution is ResolutionState.FULL
    assert audio.page_url == "https://vk.com/wall-8881618_469114"
    assert audio.variants


def test_vk_multi_video_is_also_a_collection() -> None:
    info = {
        "_type": "multi_video",
        "id": "-1_2",
        "title": "Multi-video post probe",
        "webpage_url": "https://vk.com/wall-1_2",
        "entries": [
            {
                "id": "-1_10",
                "title": "Video one",
                "webpage_url": "https://vk.com/video-1_10",
                "formats": [
                    {
                        "format_id": "mp4",
                        "ext": "mp4",
                        "vcodec": "avc1",
                        "acodec": "mp4a",
                        "height": 360,
                        "protocol": "https",
                        "url": "https://media.example.test/vk/one.mp4",
                    }
                ],
            }
        ],
    }
    source = map_fixture(info)

    assert source.kind is SourceKind.COLLECTION
    assert source.kind is not SourceKind.POST
    assert len(source.items) == 1
    assert source.items[0].kind is MediaKind.AUDIO_VIDEO
