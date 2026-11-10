"""Offline mapper tests using sanitized yt-dlp fixtures."""

# pyright: standard

from __future__ import annotations

import json
from pathlib import Path

import pytest

from trackforge.acquire.config import SourceProfile
from trackforge.acquire.errors import UnresolvedEntry, UnresolvedRedirect
from trackforge.acquire.loaders.base import LoaderContext
from trackforge.acquire.loaders.youtube import YouTubeLoader
from trackforge.acquire.loaders.ytdlp_mapper import YtDlpMapper
from trackforge.acquire.models import (
    LiveStatus,
    MediaKind,
    ResolutionState,
    SourceKind,
    SourceRef,
)
from trackforge.metadata.model import PartialDate

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> dict[str, object]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def reference(info: dict[str, object]) -> SourceRef:
    return SourceRef(
        source="youtube",
        url=str(info["webpage_url"]),
        id=str(info["id"]),
    )


def test_maps_single_video_fields() -> None:
    info = load_fixture("youtube_video.info.json")
    source = YtDlpMapper(SourceProfile(name="youtube")).map(info, reference(info))

    assert source.kind is SourceKind.SINGLE
    assert source.id == "abc123DEF45"
    assert source.title == "TrackForge Test Video"
    assert source.creator == "TrackForge Channel"
    assert source.description
    assert source.url == "https://www.youtube.com/watch?v=abc123DEF45"
    assert len(source.items) == 1
    assert source.items[0].resolution is ResolutionState.FULL


def test_auto_mode_keeps_a_single_item_full() -> None:
    info = load_fixture("youtube_video.info.json")
    source = YtDlpMapper(SourceProfile(name="yt_dlp")).map(
        info,
        reference(info),
        flat=None,
    )

    assert source.kind is SourceKind.SINGLE
    assert source.items[0].resolution is ResolutionState.FULL
    assert source.items[0].variants


def test_maps_item_and_music_fallbacks() -> None:
    info = load_fixture("youtube_video.info.json")
    item = YtDlpMapper(SourceProfile(name="youtube")).map(
        info,
        reference(info),
    ).items[0]

    assert item.id == "abc123DEF45"
    assert item.title == "TrackForge Test Video"
    assert item.creator == "TrackForge Channel"
    assert item.artists == ("TrackForge Channel",)
    assert item.duration == pytest.approx(245.5)
    assert item.suggested_stem == "TrackForge Channel - TrackForge Test Video"
    assert item.page_url == "https://www.youtube.com/watch?v=abc123DEF45"
    assert item.position == 1
    assert item.kind is MediaKind.AUDIO_VIDEO
    assert item.live_status is LiveStatus.NOT_LIVE


def test_maps_dates_to_partial_date() -> None:
    info = load_fixture("youtube_video.info.json")
    item = YtDlpMapper(SourceProfile(name="youtube")).map(
        info,
        reference(info),
    ).items[0]

    assert item.release_date == PartialDate(2024, 1, 12)

    info["release_date"] = "2023-07"
    assert isinstance(info, dict)
    item = YtDlpMapper(SourceProfile(name="youtube")).map(
        info,
        reference(info),
    ).items[0]
    assert item.release_date == PartialDate(2023, 7)


def test_malformed_dates_do_not_break_mapping() -> None:
    info = load_fixture("youtube_video.info.json")
    info["release_date"] = "not-a-date"
    info["upload_date"] = "20249999"
    item = YtDlpMapper(SourceProfile(name="youtube")).map(
        info,
        reference(info),
    ).items[0]
    assert item.release_date == PartialDate(2024)


def test_chooses_best_thumbnail_and_keeps_list() -> None:
    info = load_fixture("youtube_video.info.json")
    source = YtDlpMapper(SourceProfile(name="youtube")).map(info, reference(info))
    item = source.items[0]

    assert source.thumbnail is not None
    assert source.thumbnail.url.endswith("maxresdefault.jpg")
    assert source.thumbnail.width == 1280
    assert len(item.thumbnails) == 4
    assert item.thumbnail is not None
    assert item.thumbnail.url.endswith("maxresdefault.jpg")


def test_youtube_maxres_dimensions_are_normalized() -> None:
    info = {
        "_type": "video",
        "id": "abc",
        "title": "Thumbnail size",
        "thumbnails": [
            {
                "url": "https://i.ytimg.com/vi/abc/maxresdefault.jpg",
                "width": 1920,
                "height": 1080,
            }
        ],
    }
    item = YtDlpMapper(SourceProfile(name="youtube")).map(
        info,
        SourceRef("youtube", "https://www.youtube.com/watch?v=abc"),
    ).items[0]
    assert item.thumbnail is not None
    assert (item.thumbnail.width, item.thumbnail.height) == (1280, 720)
    assert item.thumbnail.ext == "jpg"


def test_legacy_thumbnail_field_is_normalized() -> None:
    info = {
        "_type": "video",
        "id": "abc",
        "title": "Only legacy thumbnail",
        "thumbnail": "https://example.test/cover.jpg",
    }
    item = YtDlpMapper(SourceProfile(name="yt_dlp")).map(
        info,
        SourceRef("yt-dlp", "https://example.test/x"),
    ).items[0]
    assert len(item.thumbnails) == 1
    assert item.thumbnails[0].url == "https://example.test/cover.jpg"


def test_legacy_thumbnail_list_is_expanded() -> None:
    info = {
        "_type": "video",
        "id": "abc",
        "title": "Legacy thumbnail list",
        "thumbnail": [
            "https://example.test/cover-640.jpg",
            "https://example.test/cover-320.jpg",
        ],
    }
    item = YtDlpMapper(SourceProfile(name="yt_dlp")).map(
        info,
        SourceRef("yt-dlp", "https://example.test/x"),
    ).items[0]

    assert len(item.thumbnails) == 2
    assert [thumbnail.url for thumbnail in item.thumbnails] == [
        "https://example.test/cover-640.jpg",
        "https://example.test/cover-320.jpg",
    ]
    assert item.thumbnail is not None
    assert item.thumbnail.url.startswith("https://example.test/cover-")


def test_builds_variants_and_recommendations() -> None:
    info = load_fixture("youtube_video.info.json")
    item = YtDlpMapper(SourceProfile(name="youtube")).map(
        info,
        reference(info),
    ).items[0]

    ids = {variant.id for variant in item.variants}
    assert "1080p-mp4" in ids
    assert "720p-mp4" in ids
    assert "audio-m4a-128" in ids
    assert item.recommendations.video in ids
    assert item.recommendations.audio in ids
    assert item.recommendations.video == "1080p-mp4"


def test_youtube_recommend_prefers_mp4_and_m4a() -> None:
    info = load_fixture("youtube_video.info.json")
    item = YtDlpMapper(SourceProfile(name="youtube")).map(
        info,
        reference(info),
    ).items[0]
    ctx = LoaderContext(job_id="job-1", profile=SourceProfile(name="youtube"))

    recommendations = YouTubeLoader().recommend(item, ctx=ctx)

    assert recommendations.video == "1080p-mp4"
    assert recommendations.audio == "audio-m4a-128"


def test_missing_optional_fields_are_tolerated() -> None:
    info: dict[str, object] = {"_type": "video", "id": "minimal"}
    source = YtDlpMapper(SourceProfile(name="yt_dlp")).map(
        info,
        SourceRef("yt-dlp", "https://example.test/minimal"),
    )
    item = source.items[0]
    assert item.title == "minimal"
    assert item.creator is None
    assert item.artists == ()
    assert item.duration is None
    assert item.release_date is None
    assert item.suggested_stem == "minimal"
    assert item.page_url == "https://example.test/minimal"
    assert item.variants == ()


def test_unknown_fields_go_to_extras() -> None:
    info = load_fixture("youtube_video.info.json")
    info.update(
        {
            "track_number": 3,
            "disc_number": 2,
            "genre": "Electronic",
            "custom_extractor_field": "kept?",
        }
    )
    item = YtDlpMapper(SourceProfile(name="youtube")).map(
        info,
        reference(info),
    ).items[0]

    assert item.extras["track_number"] == 3
    assert item.extras["disc_number"] == 2
    assert item.extras["genre"] == "Electronic"
    assert item.extras["custom_extractor_field"] == "kept?"


def test_flat_mapping_has_no_variants() -> None:
    info = load_fixture("youtube_video.info.json")
    item = YtDlpMapper(SourceProfile(name="youtube")).map(
        info,
        reference(info),
        flat=True,
    ).items[0]
    assert item.variants == ()
    assert item.resolution is ResolutionState.FLAT


def test_redirect_is_a_typed_error_and_empty_playlist_is_a_collection() -> None:
    mapper = YtDlpMapper(SourceProfile(name="yt_dlp"))
    ref = SourceRef("yt-dlp", "https://example.test/x")
    with pytest.raises(UnresolvedRedirect):
        mapper.map({"_type": "url", "url": "https://example.test/y"}, ref)
    with pytest.raises(UnresolvedRedirect):
        mapper.map({"_type": "url_transparent", "url": "https://example.test/y"}, ref)
    collection = mapper.map({"_type": "playlist", "entries": []}, ref)
    assert collection.kind is SourceKind.COLLECTION
    assert collection.items == ()


def test_entry_without_page_url_is_a_typed_error() -> None:
    mapper = YtDlpMapper(SourceProfile(name="yt_dlp"))
    with pytest.raises(UnresolvedEntry):
        mapper.map(
            {
                "_type": "playlist",
                "entries": [{"_type": "url", "id": "no-url"}],
            },
            SourceRef("yt-dlp", "https://example.test/playlist"),
        )


def test_maps_all_subtitle_and_automatic_caption_tracks() -> None:
    info = load_fixture("youtube_video.info.json")
    item = YtDlpMapper(SourceProfile(name="youtube")).map(
        info,
        reference(info),
    ).items[0]

    assert len(item.subtitles) == 6
    manual = [track for track in item.subtitles if not track.is_automatic]
    automatic = [track for track in item.subtitles if track.is_automatic]
    assert {(track.language, track.ext) for track in manual} == {
        ("en", "vtt"),
        ("en", "srv1"),
        ("ru", "vtt"),
    }
    assert {(track.language, track.ext) for track in automatic} == {
        ("en", "json3"),
        ("en", "ttml"),
        ("de", "vtt"),
    }
    vtt = next(
        track
        for track in manual
        if track.language == "en" and track.ext == "vtt"
    )
    assert vtt.name == "English"
    assert vtt.mime == "text/vtt"
    assert vtt.url == "https://media.example.test/subs/en.vtt"


def test_subtitle_inline_data_is_kept() -> None:
    info = {
        "_type": "video",
        "id": "abc",
        "title": "Subtitles",
        "subtitles": {
            "en": [{"ext": "vtt", "data": "WEBVTT\n\n00:00.000 --> 00:01.000\nHi"}]
        },
    }
    item = YtDlpMapper(SourceProfile(name="yt_dlp")).map(
        info,
        SourceRef("yt-dlp", "https://example.test/x"),
    ).items[0]
    assert len(item.subtitles) == 1
    assert item.subtitles[0].data is not None
    assert item.subtitles[0].url is None


def test_maps_live_stream_status_and_keeps_live_metadata() -> None:
    info = {
        "_type": "video",
        "id": "live123",
        "title": "Live radio",
        "uploader": "Radio",
        "live_status": "is_live",
        "is_live": True,
        "was_live": False,
        "concurrent_view_count": 1234,
        "webpage_url": "https://www.youtube.com/live/live123",
        "formats": [
            {
                "format_id": "95",
                "ext": "mp4",
                "vcodec": "avc1",
                "acodec": "mp4a",
                "height": 720,
                "protocol": "m3u8_native",
                "url": "https://media.example.test/live/95.m3u8",
            }
        ],
    }
    item = YtDlpMapper(SourceProfile(name="youtube")).map(
        info,
        SourceRef("youtube", "https://www.youtube.com/live/live123"),
    ).items[0]
    assert item.live_status is LiveStatus.LIVE
    assert item.duration is None
    assert item.variants[0].downloadable is True
    assert item.variants[0].streams[0].protocol == "m3u8_native"
    assert item.extras["is_live"] is True
    assert item.extras["concurrent_view_count"] == 1234


def test_infers_live_status_without_live_status_field() -> None:
    for info, expected in (
        ({"is_live": True}, LiveStatus.LIVE),
        ({"is_upcoming": True}, LiveStatus.UPCOMING),
        ({"was_live": True}, LiveStatus.WAS_LIVE),
        ({"live_status": "post_live"}, LiveStatus.POST_LIVE),
        ({}, LiveStatus.UNKNOWN),
    ):
        base = {"_type": "video", "id": "x", "title": "x", **info}
        item = YtDlpMapper(SourceProfile(name="yt_dlp")).map(
            base,
            SourceRef("yt-dlp", "https://example.test/x"),
        ).items[0]
        assert item.live_status is expected
