"""Tests for the frozen ``SourceInfo`` v1 JSON schema."""

# pyright: standard

from __future__ import annotations

import json
from dataclasses import replace

from fakes import make_source_info

from trackforge.acquire.models import (
    DownloadVariant,
    MediaItem,
    MediaKind,
    ResolutionState,
    SourceInfo,
    SourceKind,
    StreamInfo,
    StreamRole,
    SubtitleTrack,
    Thumbnail,
)
from trackforge.acquire.serialization import (
    MEDIA_ITEM_FIELDS,
    RECOMMENDATION_FIELDS,
    SOURCE_INFO_FIELDS,
    SOURCE_INFO_SCHEMA_VERSION,
    THUMBNAIL_FIELDS,
    VARIANT_FIELDS,
    assert_schema_shape,
    serialize_source_info,
    source_info_to_dict,
    source_info_to_json,
)


def test_schema_version_is_one() -> None:
    info = make_source_info()

    assert SOURCE_INFO_SCHEMA_VERSION == 1
    assert SourceInfo.SCHEMA_VERSION == 1
    assert info.schema_version == 1


def test_top_level_and_nested_keys_are_frozen() -> None:
    payload = source_info_to_dict(make_source_info())

    assert tuple(payload) == SOURCE_INFO_FIELDS
    assert set(payload) == set(SOURCE_INFO_FIELDS)
    item = payload["items"][0]
    assert isinstance(item, dict)
    assert tuple(item) == MEDIA_ITEM_FIELDS
    variant = item["variants"][0]
    assert isinstance(variant, dict)
    assert tuple(variant) == VARIANT_FIELDS
    thumbnail = payload["thumbnail"]
    assert isinstance(thumbnail, dict)
    assert tuple(thumbnail) == THUMBNAIL_FIELDS
    recommendations = item["recommendations"]
    assert isinstance(recommendations, dict)
    assert tuple(recommendations) == RECOMMENDATION_FIELDS

    assert_schema_shape(payload)


def test_scalar_values_match_the_model() -> None:
    payload = source_info_to_dict(make_source_info())

    assert payload["source"] == "fake"
    assert payload["kind"] == "single"
    assert payload["title"] == "Fake media"
    assert payload["creator"] == "Fake creator"
    assert payload["thumbnail"] == {
        "url": "https://example.test/cover.jpg",
        "width": 1280,
        "height": 720,
    }
    assert payload["warnings"] == []
    item = payload["items"][0]
    assert item["id"] == "item-1"
    assert item["page_url"] == "https://example.test/media/1"
    assert item["resolution"] == "full"
    assert item["recommendations"] == {
        "audio": "audio-best",
        "video": None,
    }
    variant = item["variants"][0]
    assert variant == {
        "id": "audio-best",
        "label": "audio original",
        "kind": "audio",
        "container": "m4a",
        "height": None,
        "filesize": 4_100_000,
        "requires_merge": False,
    }


def test_flat_items_have_no_variants() -> None:
    info = make_source_info(kind=SourceKind.COLLECTION)
    item = replace(
        info.items[0],
        resolution=ResolutionState.FLAT,
        variants=(),
    )
    info = replace(info, items=(item,))

    payload = source_info_to_dict(info)

    assert payload["kind"] == "collection"
    assert payload["items"][0]["resolution"] == "flat"
    assert payload["items"][0]["variants"] == []
    assert_schema_shape(payload)


def test_missing_thumbnail_is_null() -> None:
    info = make_source_info()
    item = replace(info.items[0], thumbnail=None)
    info = replace(info, thumbnail=None, items=(item,))

    payload = source_info_to_dict(info)

    assert payload["thumbnail"] is None
    assert payload["items"][0]["thumbnail"] is None


def test_video_variant_height_and_approximate_filesize() -> None:
    stream = StreamInfo(
        role=StreamRole.VIDEO,
        format_id="137",
        codec="avc1.640028",
        ext="mp4",
        height=1080,
        filesize_approx=48_000_000,
    )
    variant = DownloadVariant(
        id="1080p-mp4",
        label="1080p MP4",
        kind=MediaKind.VIDEO,
        container="mp4",
        streams=(stream,),
        filesize_approx=48_000_000,
    )
    info = make_source_info()
    item = replace(info.items[0], variants=(variant,))
    info = replace(info, items=(item,))

    serialized = source_info_to_dict(info)["items"][0]["variants"][0]

    assert serialized["height"] == 1080
    assert serialized["filesize"] == 48_000_000
    assert serialized["requires_merge"] is False


def test_json_text_is_valid_and_matches_dict() -> None:
    info = make_source_info()

    text = source_info_to_json(info)
    payload = json.loads(text)

    assert payload == source_info_to_dict(info)
    assert info.to_json() == text


def test_model_helpers_and_aliases_agree() -> None:
    info = make_source_info()

    assert info.to_dict() == source_info_to_dict(info)
    assert serialize_source_info(info) == source_info_to_dict(info)
    assert source_info_to_json(info, indent=None) == json.dumps(
        source_info_to_dict(info),
        ensure_ascii=False,
        indent=None,
    )


def test_secrets_and_raw_payload_fields_never_reach_json() -> None:
    secret = "SECRET-VALUE"
    thumbnail = Thumbnail(
        url="https://cdn.test/cover.jpg?token=SECRET-VALUE&sig=SECRET-VALUE",
        width=640,
        height=360,
        http_headers={"Authorization": f"Bearer {secret}"},
    )
    stream = StreamInfo(
        role=StreamRole.AUDIO,
        format_id="140",
        codec="aac",
        ext="m4a",
        url=f"https://cdn.test/audio.m4a?token={secret}",
    )
    variant = DownloadVariant(
        id="audio-m4a",
        label="Audio M4A",
        kind=MediaKind.AUDIO,
        container="m4a",
        streams=(stream,),
        selector=f"140/{secret}",
    )
    subtitle = SubtitleTrack(
        language="en",
        url=f"https://cdn.test/en.vtt?token={secret}",
        ext="vtt",
    )
    item = MediaItem(
        id="item-1",
        title="Song",
        page_url=(
            f"https://example.test/watch?v=1&token={secret}"
            f"&sig={secret}&expires=4102444800"
        ),
        thumbnail=thumbnail,
        thumbnails=(thumbnail,),
        subtitles=(subtitle,),
        variants=(variant,),
        extras={"api_token": secret, "raw": {"cookie": secret}},
    )
    info = SourceInfo(
        source="example",
        url=f"https://example.test/source?token={secret}",
        kind=SourceKind.SINGLE,
        id="item-1",
        title="Song",
        creator="Artist",
        thumbnail=thumbnail,
        items=(item,),
        warnings=(f"https://example.test/?token={secret}",),
    )

    text = source_info_to_json(info)

    assert secret not in text
    assert "http_headers" not in text
    assert "Authorization" not in text
    assert "cookies" not in text
    assert '"selector"' not in text
    assert '"extras"' not in text
    assert '"subtitles"' not in text
    assert '"streams"' not in text
    assert "<redacted>" in text
