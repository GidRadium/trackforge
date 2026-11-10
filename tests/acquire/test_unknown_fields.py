"""Unknown or malformed yt-dlp fields must never break mapping."""

# pyright: standard

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from trackforge.acquire.config import SourceProfile
from trackforge.acquire.loaders.ytdlp_mapper import YtDlpMapper
from trackforge.acquire.models import MediaKind, SourceKind, SourceRef

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def map_info(info: dict[str, Any], *, flat: bool = False):
    return YtDlpMapper(SourceProfile(name="yt_dlp")).map(
        info,
        SourceRef("yt-dlp", str(info["webpage_url"]), str(info.get("id") or "")),
        flat=flat,
    )


def first_entry(info: dict[str, Any]) -> dict[str, Any]:
    entries = info.get("entries")
    assert isinstance(entries, list) and entries
    entry = entries[0]
    assert isinstance(entry, dict)
    return entry


@pytest.mark.parametrize(
    ("name", "flat"),
    [
        ("vimeo_video.info.json", False),
        ("vimeo_playlist.info.json", True),
        ("bandcamp_track.info.json", False),
        ("bandcamp_album.info.json", False),
        ("pinterest_pin.info.json", False),
        ("vk_wall.info.json", False),
    ],
)
def test_unknown_fields_on_every_site_are_tolerated(
    name: str,
    flat: bool,
) -> None:
    info = load_fixture(name)
    target = first_entry(info) if info.get("_type") in {"playlist", "multi_video"} else info
    target["future_extractor_field"] = {"nested": [1, 2, 3]}
    target["another_unknown_field"] = "kept in extras"

    source = map_info(info, flat=flat)

    assert source.items
    if not flat:
        assert source.items[0].extras["future_extractor_field"] == {
            "nested": [1, 2, 3]
        }
        assert source.items[0].extras["another_unknown_field"] == "kept in extras"


def test_private_and_transport_fields_are_not_copied_to_extras() -> None:
    info = {
        "_type": "video",
        "id": "unknown-fields",
        "title": "Unknown fields",
        "webpage_url": "https://example.test/unknown",
        "_private": "hidden",
        "__post_extractor": "hidden",
        "cookies": "sessionid=secret",
        "http_headers": {"Authorization": "Bearer secret"},
        "requested_formats": [{"url": "https://media.example.test/x"}],
        "fragments": [{"url": "https://media.example.test/fragment"}],
        "manifest_url": "https://media.example.test/manifest.m3u8",
        "format": "best",
        "format_id": "best",
        "future_field": [None, True, 1, "text"],
        "formats": [
            {
                "format_id": "best",
                "ext": "mp4",
                "vcodec": "avc1",
                "acodec": "mp4a",
                "height": 720,
                "protocol": "https",
                "url": "https://media.example.test/video.mp4",
                "future_format_field": "ignored",
            }
        ],
    }
    item = map_info(info).items[0]

    assert item.extras["future_field"] == [None, True, 1, "text"]
    for skipped in (
        "_private",
        "__post_extractor",
        "cookies",
        "http_headers",
        "requested_formats",
        "fragments",
        "manifest_url",
        "format",
        "format_id",
    ):
        assert skipped not in item.extras
    assert item.variants
    assert item.variants[0].kind is MediaKind.AUDIO_VIDEO


def test_unknown_top_level_type_is_treated_as_single() -> None:
    info = {
        "_type": "future_media_type",
        "id": "future-1",
        "title": "Future media",
        "webpage_url": "https://example.test/future/1",
        "thumbnails": [
            {"url": "https://example.test/cover.jpg", "width": 640, "height": 480}
        ],
    }
    source = map_info(info)

    assert source.kind is SourceKind.SINGLE
    assert source.items[0].title == "Future media"
    assert source.items[0].kind is MediaKind.UNKNOWN


def test_malformed_thumbnails_are_skipped_without_raising() -> None:
    info = {
        "_type": "video",
        "id": "bad-thumbs",
        "title": "Bad thumbnails",
        "webpage_url": "https://example.test/bad-thumbs",
        "thumbnails": [
            None,
            {"width": 100},
            {"url": 42, "width": "wide", "height": -1, "preference": "high"},
            {"url": "https://example.test/good.jpg", "width": "1280", "height": "720"},
            "https://example.test/legacy.jpg",
        ],
        "thumbnail": {"url": None},
    }
    source = map_info(info)
    item = source.items[0]

    urls = {thumbnail.url for thumbnail in item.thumbnails}
    assert "https://example.test/good.jpg" in urls
    assert "https://example.test/legacy.jpg" in urls
    assert item.thumbnail is not None
    assert item.thumbnail.url == "https://example.test/good.jpg"


def test_malformed_numbers_and_extra_types_do_not_raise() -> None:
    info = {
        "_type": "video",
        "id": "bad-numbers",
        "title": "Bad numbers",
        "webpage_url": "https://example.test/bad-numbers",
        "duration": "not-a-number",
        "release_date": "2024-99-99",
        "timestamp": "nope",
        "formats": [
            {
                "format_id": "bad id !!",
                "ext": "mp4",
                "vcodec": "avc1",
                "acodec": "mp4a",
                "height": "1080",
                "filesize": "not-a-size",
                "protocol": "https",
                "url": "https://media.example.test/bad-numbers.mp4",
            }
        ],
    }
    source = map_info(info)
    item = source.items[0]

    assert item.duration is None
    assert item.release_date is not None
    assert item.variants
    assert item.variants[0].id == "1080p-mp4"
    assert item.variants[0].streams[0].height == 1080
