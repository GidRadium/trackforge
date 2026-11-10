"""Frozen ``SourceInfo`` v1 JSON serialization.

Only the fields listed here are part of the public inspect contract.  The
serializer is deliberately explicit: it never iterates dataclass fields and
never forwards ``extras``, stream URLs, HTTP headers, subtitles or raw yt-dlp
payloads.  A new public field therefore requires a conscious change to this
module and a new :data:`SOURCE_INFO_SCHEMA_VERSION`.
"""

# pyright: standard

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import TypedDict

from .models import (
    SOURCE_INFO_SCHEMA_VERSION,
    DownloadVariant,
    MediaItem,
    Recommendations,
    SourceInfo,
    StreamRole,
    Thumbnail,
)
from .redaction import redact_text

SOURCE_INFO_FIELDS: tuple[str, ...] = (
    "schema_version",
    "source",
    "kind",
    "title",
    "creator",
    "thumbnail",
    "items",
    "warnings",
)

MEDIA_ITEM_FIELDS: tuple[str, ...] = (
    "id",
    "title",
    "page_url",
    "resolution",
    "thumbnail",
    "recommendations",
    "variants",
)

VARIANT_FIELDS: tuple[str, ...] = (
    "id",
    "label",
    "kind",
    "container",
    "height",
    "filesize",
    "requires_merge",
)

THUMBNAIL_FIELDS: tuple[str, ...] = ("url", "width", "height")

RECOMMENDATION_FIELDS: tuple[str, ...] = ("audio", "video")


class ThumbnailDict(TypedDict):
    """Serialized thumbnail: URL and dimensions only."""

    url: str
    width: int | None
    height: int | None


class RecommendationsDict(TypedDict):
    """Serialized recommendations for schema v1."""

    audio: str | None
    video: str | None


class VariantDict(TypedDict):
    """Serialized download variant without URLs or selectors."""

    id: str
    label: str
    kind: str
    container: str | None
    height: int | None
    filesize: int | None
    requires_merge: bool


class MediaItemDict(TypedDict):
    """Serialized media item for schema v1."""

    id: str
    title: str
    page_url: str | None
    resolution: str
    thumbnail: ThumbnailDict | None
    recommendations: RecommendationsDict
    variants: list[VariantDict]


class SourceInfoDict(TypedDict):
    """Top-level frozen schema-version-1 document."""

    schema_version: int
    source: str
    kind: str
    title: str | None
    creator: str | None
    thumbnail: ThumbnailDict | None
    items: list[MediaItemDict]
    warnings: list[str]


def _public_url(value: str) -> str:
    """Return a display-safe URL without embedded credentials/tokens."""
    return redact_text(value)


def _thumbnail(value: Thumbnail | None) -> ThumbnailDict | None:
    if value is None:
        return None
    url = value.url.strip()
    if not url:
        return None
    return {
        "url": _public_url(url),
        "width": value.width,
        "height": value.height,
    }


def _variant_height(variant: DownloadVariant) -> int | None:
    heights = [
        stream.height
        for stream in variant.streams
        if stream.role is StreamRole.VIDEO and stream.height is not None
    ]
    return max(heights) if heights else None


def _variant_filesize(variant: DownloadVariant) -> int | None:
    if variant.filesize is not None:
        return variant.filesize
    return variant.filesize_approx


def _recommendations(value: Recommendations) -> RecommendationsDict:
    return {
        "audio": value.audio,
        "video": value.video,
    }


def _variant(value: DownloadVariant) -> VariantDict:
    return {
        "id": value.id,
        "label": value.label,
        "kind": value.kind.value,
        "container": value.container,
        "height": _variant_height(value),
        "filesize": _variant_filesize(value),
        "requires_merge": value.requires_merge,
    }


def _item(value: MediaItem) -> MediaItemDict:
    return {
        "id": value.id,
        "title": value.title,
        "page_url": _public_url(value.page_url) if value.page_url else None,
        "resolution": value.resolution.value,
        "thumbnail": _thumbnail(value.thumbnail),
        "recommendations": _recommendations(value.recommendations),
        "variants": [_variant(variant) for variant in value.variants],
    }


def source_info_to_dict(source: SourceInfo) -> SourceInfoDict:
    """Serialize a :class:`SourceInfo` as the frozen schema-version-1 object."""
    return {
        "schema_version": SOURCE_INFO_SCHEMA_VERSION,
        "source": source.source,
        "kind": source.kind.value,
        "title": source.title,
        "creator": source.creator,
        "thumbnail": _thumbnail(source.thumbnail),
        "items": [_item(item) for item in source.items],
        "warnings": [
            redact_text(warning) for warning in source.warnings
        ],
    }


def source_info_to_json(
    source: SourceInfo,
    *,
    indent: int | None = 2,
) -> str:
    """Serialize a :class:`SourceInfo` to stable, secret-free JSON text."""
    return json.dumps(
        source_info_to_dict(source),
        ensure_ascii=False,
        indent=indent,
    )


def assert_schema_shape(payload: Mapping[str, object]) -> None:
    """Validate the top-level shape used by tests and integrations.

    This is intentionally a lightweight contract check rather than a full
    JSON-schema implementation; it catches accidental field additions to the
    frozen surface without adding a runtime dependency.
    """
    expected = set(SOURCE_INFO_FIELDS)
    actual = set(payload)
    if actual != expected:
        raise ValueError(
            f"SourceInfo v1 keys changed: expected {sorted(expected)}, "
            f"got {sorted(actual)}"
        )
    items = payload.get("items")
    if not isinstance(items, list):
        raise TypeError("SourceInfo v1 'items' must be a list")
    for item in items:
        if not isinstance(item, Mapping):
            raise TypeError("SourceInfo v1 items must be objects")
        _check_keys(item, MEDIA_ITEM_FIELDS, "item")
        variants = item.get("variants")
        if not isinstance(variants, list):
            raise TypeError("SourceInfo v1 'variants' must be a list")
        for variant in variants:
            if not isinstance(variant, Mapping):
                raise TypeError("SourceInfo v1 variants must be objects")
            _check_keys(variant, VARIANT_FIELDS, "variant")


def _check_keys(
    value: Mapping[str, object],
    expected: Sequence[str],
    label: str,
) -> None:
    if set(value) != set(expected):
        raise ValueError(
            f"SourceInfo v1 {label} keys changed: "
            f"expected {sorted(expected)}, got {sorted(value)}"
        )


# Friendly aliases for integrations that do not want to guess a name.
to_dict = source_info_to_dict
to_json = source_info_to_json
serialize_source_info = source_info_to_dict

__all__ = [
    "MEDIA_ITEM_FIELDS",
    "RECOMMENDATION_FIELDS",
    "SOURCE_INFO_FIELDS",
    "SOURCE_INFO_SCHEMA_VERSION",
    "THUMBNAIL_FIELDS",
    "VARIANT_FIELDS",
    "assert_schema_shape",
    "serialize_source_info",
    "source_info_to_dict",
    "source_info_to_json",
    "to_dict",
    "to_json",
]
