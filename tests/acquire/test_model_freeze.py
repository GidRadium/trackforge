"""Freeze checks for the public ``SourceInfo`` v1 model and JSON schema."""

# pyright: standard

from __future__ import annotations

import json
from dataclasses import fields, replace
from pathlib import Path
from typing import Any

import pytest

from trackforge.acquire.config import SourceProfile
from trackforge.acquire.loaders.ytdlp_mapper import YtDlpMapper
from trackforge.acquire.models import (
    DownloadVariant,
    MediaItem,
    MediaKind,
    Recommendations,
    ResolutionState,
    SourceInfo,
    SourceKind,
    SourceRef,
    StreamInfo,
    StreamRole,
    Thumbnail,
    UrlHint,
)
from trackforge.acquire.serialization import (
    MEDIA_ITEM_FIELDS,
    RECOMMENDATION_FIELDS,
    SOURCE_INFO_FIELDS,
    SOURCE_INFO_SCHEMA_VERSION,
    THUMBNAIL_FIELDS,
    VARIANT_FIELDS,
    assert_schema_shape,
    source_info_to_dict,
)

FIXTURES = Path(__file__).parent / "fixtures"

SOURCE_INFO_V1_FIELDS = (
    "source",
    "url",
    "kind",
    "id",
    "title",
    "creator",
    "description",
    "thumbnails",
    "items",
    "warnings",
    "thumbnail",
    "publisher",
)
MEDIA_ITEM_V1_FIELDS = (
    "id",
    "title",
    "suggested_stem",
    "kind",
    "creator",
    "artists",
    "album",
    "album_artists",
    "duration",
    "live_status",
    "release_date",
    "page_url",
    "thumbnails",
    "thumbnail",
    "subtitles",
    "variants",
    "recommendations",
    "resolution",
    "position",
    "group_id",
    "extras",
)
VARIANT_V1_FIELDS = (
    "id",
    "label",
    "kind",
    "container",
    "streams",
    "selector",
    "filesize",
    "filesize_approx",
    "requires_merge",
    "requires_ffmpeg",
    "downloadable",
    "unavailable_reason",
)
THUMBNAIL_V1_FIELDS = (
    "url",
    "width",
    "height",
    "ext",
    "mime",
    "filesize",
    "preference",
    "source",
    "http_headers",
)
RECOMMENDATIONS_V1_FIELDS = ("audio", "video", "image", "document")
STREAM_INFO_V1_FIELDS = (
    "role",
    "format_id",
    "codec",
    "ext",
    "url",
    "width",
    "height",
    "fps",
    "bitrate_kbps",
    "sample_rate",
    "channels",
    "language",
    "language_preference",
    "dynamic_range",
    "filesize",
    "filesize_approx",
    "protocol",
    "drm",
)


def _field_names(value: type) -> tuple[str, ...]:
    return tuple(item.name for item in fields(value))


def test_model_fields_are_frozen() -> None:
    assert _field_names(SourceInfo) == SOURCE_INFO_V1_FIELDS
    assert _field_names(MediaItem) == MEDIA_ITEM_V1_FIELDS
    assert _field_names(DownloadVariant) == VARIANT_V1_FIELDS
    assert _field_names(Thumbnail) == THUMBNAIL_V1_FIELDS
    assert _field_names(Recommendations) == RECOMMENDATIONS_V1_FIELDS
    assert _field_names(StreamInfo) == STREAM_INFO_V1_FIELDS


def test_schema_field_names_are_frozen() -> None:
    assert SOURCE_INFO_SCHEMA_VERSION == 1
    assert SOURCE_INFO_FIELDS == (
        "schema_version",
        "source",
        "kind",
        "title",
        "creator",
        "thumbnail",
        "items",
        "warnings",
    )
    assert MEDIA_ITEM_FIELDS == (
        "id",
        "title",
        "page_url",
        "resolution",
        "thumbnail",
        "recommendations",
        "variants",
    )
    assert VARIANT_FIELDS == (
        "id",
        "label",
        "kind",
        "container",
        "height",
        "filesize",
        "requires_merge",
    )
    assert THUMBNAIL_FIELDS == ("url", "width", "height")
    assert RECOMMENDATION_FIELDS == ("audio", "video")


def test_public_enum_values_are_frozen() -> None:
    assert {kind.value for kind in SourceKind} == {
        "single",
        "post",
        "collection",
    }
    assert {hint.value for hint in UrlHint} == {
        "single",
        "post",
        "collection",
        "unknown",
    }
    assert {state.value for state in ResolutionState} == {
        "full",
        "flat",
        "unresolved",
    }
    assert {kind.value for kind in MediaKind} == {
        "audio",
        "video",
        "audio_video",
        "image",
        "animated_image",
        "document",
        "unknown",
    }
    assert {role.value for role in StreamRole} == {
        "video",
        "audio",
        "image",
        "document",
    }


def load_fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def map_fixture(info: dict[str, Any], *, flat: bool) -> SourceInfo:
    url = str(info.get("webpage_url") or "https://example.test/source")
    ref = SourceRef("yt-dlp", url, str(info.get("id") or ""))
    return YtDlpMapper(SourceProfile(name="yt_dlp")).map(
        info,
        ref,
        flat=flat,
    )


FIXTURE_NAMES = tuple(sorted(path.name for path in FIXTURES.glob("*.json")))


@pytest.mark.parametrize("name", FIXTURE_NAMES)
@pytest.mark.parametrize("flat", [True, False], ids=["flat", "full"])
def test_every_fixture_maps_and_serializes(name: str, flat: bool) -> None:
    info = load_fixture(name)

    source = map_fixture(info, flat=flat)
    payload = source_info_to_dict(source)
    text = json.dumps(payload, ensure_ascii=False)

    assert_schema_shape(payload)
    assert source.source == "yt-dlp"
    assert source.items
    assert all(item.id and item.title and item.page_url for item in source.items)
    for item in source.items:
        if flat:
            assert item.resolution is ResolutionState.FLAT
            assert item.variants == ()
        assert all(variant.id for variant in item.variants)
    if source.kind is SourceKind.SINGLE:
        assert len(source.items) == 1
    assert "info_dict" not in text
    assert "http_headers" not in text


FORBIDDEN_KEYS = frozenset(
    {
        "cookies",
        "http_headers",
        "manifest_url",
        "requested_formats",
        "fragments",
        "requested_subtitles",
        "extras",
        "subtitles",
        "streams",
        "selector",
        "info_dict",
    }
)


def _walk_keys(value: object):
    if isinstance(value, dict):
        for key, item in value.items():
            yield str(key)
            yield from _walk_keys(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_keys(item)


@pytest.mark.parametrize("name", FIXTURE_NAMES)
def test_serialized_fixtures_never_expose_transport_or_raw_fields(name: str) -> None:
    source = map_fixture(load_fixture(name), flat=False)
    text = json.dumps(source_info_to_dict(source), ensure_ascii=False)

    assert not (set(_walk_keys(source_info_to_dict(source))) & FORBIDDEN_KEYS)
    for marker in ("http_headers", "cookies", "Authorization", "info_dict"):
        assert marker not in text


def test_unknown_fields_go_to_extras_and_never_to_json() -> None:
    info = load_fixture("youtube_video.info.json")
    info = dict(info)
    info["brand_new_field"] = {"nested": "value"}
    info["cookies"] = "SID=SECRET"
    info["http_headers"] = {"Authorization": "Bearer SECRET"}

    source = map_fixture(info, flat=False)
    item = source.items[0]

    assert item.extras["brand_new_field"] == {"nested": "value"}
    assert "cookies" not in item.extras
    assert "http_headers" not in item.extras
    text = json.dumps(source_info_to_dict(source), ensure_ascii=False)
    assert "brand_new_field" not in text
    assert "SECRET" not in text


def test_flat_mode_is_a_schema_state_not_a_different_model() -> None:
    source = map_fixture(load_fixture("youtube_playlist_flat.info.json"), flat=True)
    payload = source_info_to_dict(source)

    assert payload["kind"] == "collection"
    assert all(item["resolution"] == "flat" for item in payload["items"])
    assert all(item["variants"] == [] for item in payload["items"])


def test_full_collection_keeps_variants_in_schema() -> None:
    source = map_fixture(load_fixture("youtube_playlist_full.info.json"), flat=False)
    payload = source_info_to_dict(source)

    assert payload["kind"] == "collection"
    assert any(item["variants"] for item in payload["items"])
    assert all(item["resolution"] == "full" for item in payload["items"])


def test_model_identity_and_recommendations_survive_freeze_copy(
    source_info: SourceInfo,
) -> None:
    copied = replace(source_info)

    assert copied == source_info
    assert copied.ref() == source_info.ref()
