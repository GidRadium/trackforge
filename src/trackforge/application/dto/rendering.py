"""Render options, canonical tag sets, and render results.

The canonical model is defined once in ``08-metadata.md``; this module is the
application-facing projection of it.  ``TagSet`` never depends on a concrete
tag format: adapters translate the same values to ID3, MP4 atoms, or Vorbis
comments.
"""

from __future__ import annotations

import re
import struct
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from trackforge.domain.errors import DomainValidationError
from trackforge.domain.model._validation import (
    FieldValue,
    ensure_datetime,
    utc_now,
)
from trackforge.domain.model.ids import RenderId
from trackforge.domain.model.outputs import (
    OutputBundle,
    Render,
    ResolvedMetadata,
    TargetFormat,
)

TAG_MAPPING_VERSION = "canonical-v2"
RENDER_NAMING_TEMPLATE = "track.{ext}"

COVER_MAX_BYTES = 10 * 1024 * 1024
COVER_MAX_DIMENSION = 4_000
COVER_SIDECAR_NAME = "cover.jpg"
LRC_SIDECAR_SUFFIX = ".lrc"
ELRC_SIDECAR_SUFFIX = ".elrc"
SIDECAR_NAME = "trackforge.json"


def lyrics_sidecar_name(audio_name: str, *, enhanced: bool = False) -> str:
    """Return the sibling sidecar name for an audio basename.

    ``track.mp3`` becomes ``track.mp3.lrc`` (or ``.elrc``).  Publication later
    renames the pair to the library stem, but the bundle itself keeps the
    audio basename so the sidecar is unambiguous.
    """

    if not isinstance(audio_name, str) or not audio_name.strip():
        raise DomainValidationError("audio_name must be a non-empty string")
    suffix = ELRC_SIDECAR_SUFFIX if enhanced else LRC_SIDECAR_SUFFIX
    return f"{audio_name}{suffix}"


_SUPPORTED_EXTENSIONS: dict[TargetFormat, str] = {
    TargetFormat.MP3: "mp3",
    TargetFormat.M4A: "m4a",
    TargetFormat.FLAC: "flac",
    TargetFormat.OGG: "ogg",
    TargetFormat.OPUS: "opus",
}

# Containers that can be treated as one of the five canonical output formats.
_CONTAINER_TARGETS: dict[str, TargetFormat] = {
    "mp3": TargetFormat.MP3,
    "m4a": TargetFormat.M4A,
    "m4b": TargetFormat.M4A,
    "mp4": TargetFormat.M4A,
    "alac": TargetFormat.M4A,
    "flac": TargetFormat.FLAC,
    "ogg": TargetFormat.OGG,
    "oga": TargetFormat.OGG,
    "opus": TargetFormat.OPUS,
}

_SYNCED_LYRIC_RE = re.compile(r"(?m)^\s*\[\d{1,3}:\d{2}(?:[.:]\d{1,3})?\]")
_TRACKFORGE_KEY_RE = re.compile(r"^TRACKFORGE_[A-Z0-9_]+$")


def target_format_for(
    container: str | TargetFormat,
    requested: TargetFormat | str | None = None,
) -> TargetFormat:
    """Resolve the output format for a source container.

    ``None`` keeps a supported source container and falls back to FLAC for
    containers that have no reliable tag mapping in v1.
    """

    if requested is not None:
        return _coerce_target(requested, "target_format")
    key = container.value if isinstance(container, TargetFormat) else str(container).lower()
    return _CONTAINER_TARGETS.get(key, TargetFormat.FLAC)


def is_supported_container(container: str | TargetFormat) -> bool:
    """Return whether ``container`` has a native target mapping in v1."""

    key = container.value if isinstance(container, TargetFormat) else str(container).lower()
    return key in _CONTAINER_TARGETS


def extension_for(target: TargetFormat) -> str:
    """Return the canonical file extension for a target format."""

    return _SUPPORTED_EXTENSIONS[_coerce_target(target, "target")]


def is_synced_lyrics(text: str | None) -> bool:
    """Return whether ``text`` looks like LRC/ELRC rather than plain lyrics."""

    return bool(text and _SYNCED_LYRIC_RE.search(text))


def _coerce_target(value: object, field_name: str) -> TargetFormat:
    if isinstance(value, TargetFormat):
        return value
    if isinstance(value, str):
        try:
            return TargetFormat(value.lower())
        except ValueError:
            pass
    raise DomainValidationError(
        f"{field_name} must be one of: mp3, m4a, flac, ogg, opus",
        context={"field": field_name, "value": str(value)},
    )


def _optional_text(value: object, field_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise DomainValidationError(
            f"{field_name} must be a string",
            context={"field": field_name, "actual_type": type(value).__name__},
        )
    text = value.strip()
    return text or None


def _text_tuple(value: object, field_name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        raw: Sequence[object] = (value,)
    elif isinstance(value, (list, tuple, set, frozenset)):
        raw = tuple(value)
    else:
        raise DomainValidationError(
            f"{field_name} must be a string or a list of strings",
            context={"field": field_name, "actual_type": type(value).__name__},
        )
    result: list[str] = []
    for index, item in enumerate(raw):
        if not isinstance(item, str):
            raise DomainValidationError(
                f"{field_name}[{index}] must be a string",
                context={"field": field_name, "index": index},
            )
        text = item.strip()
        if text and text not in result:
            result.append(text)
    return tuple(result)


def _optional_int(value: object, field_name: str, *, minimum: int = 0) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise DomainValidationError(
            f"{field_name} must be an integer",
            context={"field": field_name, "actual_type": type(value).__name__},
        )
    if value < minimum:
        raise DomainValidationError(
            f"{field_name} must be >= {minimum}",
            context={"field": field_name, "value": value},
        )
    return value


def _positive_or_none(value: object, field_name: str) -> int | None:
    number = _optional_int(value, field_name, minimum=0)
    return number if number else None


def _png_dimensions(data: bytes) -> tuple[int, int] | None:
    if len(data) < 24 or not data.startswith(b"\x89PNG\r\n\x1a\n"):
        return None
    if data[12:16] != b"IHDR":
        return None
    width, height = struct.unpack(">II", data[16:24])
    if width < 1 or height < 1:
        raise DomainValidationError("cover PNG has invalid dimensions")
    return width, height


def _optional_bpm(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise DomainValidationError(
            "bpm must be a number",
            context={"field": "bpm", "actual_type": type(value).__name__},
        )
    number = float(value)
    if number != number or number in {float("inf"), float("-inf")}:
        raise DomainValidationError("bpm must be finite", context={"field": "bpm"})
    if number < 0:
        raise DomainValidationError("bpm must not be negative", context={"field": "bpm"})
    return number if number > 0 else None


def _optional_bool(value: object, field_name: str) -> bool | None:
    if value is None:
        return None
    if not isinstance(value, bool):
        raise DomainValidationError(
            f"{field_name} must be a boolean",
            context={"field": field_name, "actual_type": type(value).__name__},
        )
    return value


_MBID_ALIASES = {
    "recording": "mb_recording_id",
    "recording_id": "mb_recording_id",
    "track": "mb_recording_id",
    "track_id": "mb_recording_id",
    "musicbrainz_trackid": "mb_recording_id",
    "musicbrainz_recordingid": "mb_recording_id",
    "release": "mb_release_id",
    "release_id": "mb_release_id",
    "album": "mb_release_id",
    "album_id": "mb_release_id",
    "musicbrainz_albumid": "mb_release_id",
    "release_group": "mb_release_group_id",
    "release_group_id": "mb_release_group_id",
    "musicbrainz_releasegroupid": "mb_release_group_id",
    "artist": "mb_artist_id",
    "artist_id": "mb_artist_id",
    "musicbrainz_artistid": "mb_artist_id",
    "albumartist": "mb_albumartist_id",
    "album_artist": "mb_albumartist_id",
    "albumartist_id": "mb_albumartist_id",
    "musicbrainz_albumartistid": "mb_albumartist_id",
    "acoustid": "acoustid",
    "isrc": "isrc",
    "mb_recording_id": "mb_recording_id",
    "mb_release_id": "mb_release_id",
    "mb_release_group_id": "mb_release_group_id",
    "mb_artist_id": "mb_artist_id",
    "mb_albumartist_id": "mb_albumartist_id",
}


def _mbid_aliases(value: object) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise DomainValidationError(
            "mbids must be a mapping",
            context={"field": "mbids", "actual_type": type(value).__name__},
        )
    result: dict[str, str] = {}
    for raw_key, raw_value in value.items():
        if not isinstance(raw_key, str) or not isinstance(raw_value, str):
            raise DomainValidationError("mbids keys and values must be strings")
        key = raw_key.strip().lower().replace("-", "_").replace(" ", "_")
        field_name = _MBID_ALIASES.get(key)
        if field_name is None:
            raise DomainValidationError(
                "unknown mbids key",
                context={"field": "mbids", "key": raw_key},
            )
        text = raw_value.strip()
        if text:
            result[field_name] = text
    return result


def _trackforge_map(value: object) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise DomainValidationError(
            "trackforge must be a mapping",
            context={"field": "trackforge", "actual_type": type(value).__name__},
        )
    result: dict[str, str] = {}
    for raw_key, raw_value in value.items():
        if not isinstance(raw_key, str) or not isinstance(raw_value, str):
            raise DomainValidationError(
                "trackforge keys and values must be strings",
                context={"field": "trackforge", "key": str(raw_key)},
            )
        key = raw_key.strip().upper()
        if not _TRACKFORGE_KEY_RE.fullmatch(key):
            raise DomainValidationError(
                "trackforge keys must use the TRACKFORGE_ prefix",
                context={"field": "trackforge", "key": raw_key},
            )
        text = raw_value.strip()
        if text:
            result[key] = text
    return result


@dataclass(frozen=True, slots=True)
class CoverArt:
    """Front-cover bytes with a validated image MIME type."""

    data: bytes
    mime_type: str
    description: str = ""
    width: int | None = None
    height: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.data, (bytes, bytearray)):
            raise DomainValidationError(
                "cover data must be bytes",
                context={"actual_type": type(self.data).__name__},
            )
        data = bytes(self.data)
        if not data:
            raise DomainValidationError("cover data must not be empty")
        if len(data) > COVER_MAX_BYTES:
            raise DomainValidationError(
                "cover exceeds the maximum size",
                context={"size_bytes": len(data), "max_bytes": COVER_MAX_BYTES},
            )
        mime = self.mime_type.strip().lower()
        if mime not in {"image/jpeg", "image/png"}:
            raise DomainValidationError(
                "cover MIME type must be image/jpeg or image/png",
                context={"mime_type": self.mime_type},
            )
        object.__setattr__(self, "data", data)
        object.__setattr__(self, "mime_type", mime)
        object.__setattr__(self, "description", self.description.strip())
        width = _optional_int(self.width, "width", minimum=1)
        height = _optional_int(self.height, "height", minimum=1)
        detected = _png_dimensions(data)
        if detected is not None:
            if width is None:
                width = detected[0]
            if height is None:
                height = detected[1]
        if width is not None and width > COVER_MAX_DIMENSION:
            raise DomainValidationError(
                "cover width exceeds the maximum",
                context={"width": width, "max": COVER_MAX_DIMENSION},
            )
        if height is not None and height > COVER_MAX_DIMENSION:
            raise DomainValidationError(
                "cover height exceeds the maximum",
                context={"height": height, "max": COVER_MAX_DIMENSION},
            )
        object.__setattr__(self, "width", width)
        object.__setattr__(self, "height", height)

    @classmethod
    def from_path(cls, path: Path, *, description: str = "") -> CoverArt:
        """Read a JPEG or PNG file, detecting the MIME type from its magic."""

        data = Path(path).read_bytes()
        if data.startswith(b"\x89PNG\r\n\x1a\n"):
            mime = "image/png"
        elif data.startswith(b"\xff\xd8\xff"):
            mime = "image/jpeg"
        else:
            raise DomainValidationError(
                "cover must be a JPEG or PNG image",
                context={"path": str(path)},
            )
        return cls(data=data, mime_type=mime, description=description)


@dataclass(frozen=True, slots=True)
class RenderOptions:
    """Options controlling output format and which sidecars are written."""

    target_format: TargetFormat | None = None
    id3_version: Literal[3, 4] = 4
    cover_embed: bool = True
    cover_sidecar: bool = False
    lyrics_sidecar: bool = True
    preserve_unknown_tags: bool = False

    def __post_init__(self) -> None:
        if self.target_format is not None:
            object.__setattr__(
                self,
                "target_format",
                _coerce_target(self.target_format, "target_format"),
            )
        if self.id3_version not in {3, 4}:
            raise DomainValidationError(
                "id3_version must be 3 or 4",
                context={"id3_version": self.id3_version},
            )
        for name in (
            "cover_embed",
            "cover_sidecar",
            "lyrics_sidecar",
            "preserve_unknown_tags",
        ):
            if not isinstance(getattr(self, name), bool):
                raise DomainValidationError(f"{name} must be a boolean")

    def as_mapping(self) -> dict[str, Any]:
        """Return the stable JSON representation persisted with a render."""

        return {
            "target_format": (None if self.target_format is None else self.target_format.value),
            "id3_version": self.id3_version,
            "cover_embed": self.cover_embed,
            "cover_sidecar": self.cover_sidecar,
            "lyrics_sidecar": self.lyrics_sidecar,
            "preserve_unknown_tags": self.preserve_unknown_tags,
        }


@dataclass(frozen=True, slots=True)
class TagSet:
    """Canonical values ready to be mapped into a concrete tag format.

    Absent text is ``None``; absent lists are empty.  Writers never emit empty
    values.  Synced lyrics are deliberately excluded: LRC/ELRC live only in a
    sidecar.
    """

    title: str | None = None
    artists: tuple[str, ...] = ()
    albumartist: str | None = None
    album: str | None = None
    date: str | None = None
    originaldate: str | None = None
    originaltitle: str | None = None
    remixer: str | None = None
    originalartist: str | None = None
    qualifier: str | None = None
    artwork: str | None = None
    track: int | None = None
    tracktotal: int | None = None
    disc: int | None = None
    disctotal: int | None = None
    genre: tuple[str, ...] = ()
    bpm: float | None = None
    key: str | None = None
    language: str | None = None
    label: str | None = None
    catalog_number: str | None = None
    barcode: str | None = None
    isrc: str | None = None
    compilation: bool | None = None
    comment: str | None = None
    description: str | None = None
    lyrics: str | None = None
    source_url: str | None = None
    source_id: str | None = None
    copyright: str | None = None
    license: str | None = None
    mb_recording_id: str | None = None
    mb_release_id: str | None = None
    mb_release_group_id: str | None = None
    mb_artist_id: str | None = None
    mb_albumartist_id: str | None = None
    acoustid: str | None = None
    trackforge: Mapping[str, str] = field(default_factory=dict)
    # Constructor aliases accepted for callers that use the compact canonical
    # spellings.  Values are folded into the explicit fields below.
    source: str | None = None
    catalog: str | None = None
    mbids: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in (
            "title",
            "albumartist",
            "album",
            "date",
            "originaldate",
            "originaltitle",
            "remixer",
            "originalartist",
            "qualifier",
            "artwork",
            "key",
            "language",
            "label",
            "catalog_number",
            "barcode",
            "isrc",
            "comment",
            "description",
            "lyrics",
            "source_url",
            "source_id",
            "copyright",
            "license",
            "mb_recording_id",
            "mb_release_id",
            "mb_release_group_id",
            "mb_artist_id",
            "mb_albumartist_id",
            "acoustid",
        ):
            object.__setattr__(self, name, _optional_text(getattr(self, name), name))
        object.__setattr__(self, "artists", _text_tuple(self.artists, "artists"))
        object.__setattr__(self, "genre", _text_tuple(self.genre, "genre"))
        object.__setattr__(self, "track", _positive_or_none(self.track, "track"))
        object.__setattr__(
            self,
            "tracktotal",
            _positive_or_none(self.tracktotal, "tracktotal"),
        )
        object.__setattr__(self, "disc", _positive_or_none(self.disc, "disc"))
        object.__setattr__(
            self,
            "disctotal",
            _positive_or_none(self.disctotal, "disctotal"),
        )
        object.__setattr__(self, "bpm", _optional_bpm(self.bpm))
        object.__setattr__(
            self,
            "compilation",
            _optional_bool(self.compilation, "compilation"),
        )
        object.__setattr__(self, "trackforge", _trackforge_map(self.trackforge))
        object.__setattr__(self, "source", _optional_text(self.source, "source"))
        object.__setattr__(self, "catalog", _optional_text(self.catalog, "catalog"))
        object.__setattr__(self, "mbids", _mbid_aliases(self.mbids))
        if self.source_url is None and self.source is not None:
            object.__setattr__(self, "source_url", self.source)
        if self.source_url is not None:
            from trackforge.application.services.sidecar import sanitize_source_url

            object.__setattr__(
                self,
                "source_url",
                sanitize_source_url(self.source_url),
            )
            object.__setattr__(self, "source", self.source_url)
        if self.catalog_number is None and self.catalog is not None:
            object.__setattr__(self, "catalog_number", self.catalog)
        if self.source_url is not None or self.source_id is not None:
            trackforge = dict(self.trackforge)
            if self.source_url is not None:
                trackforge["TRACKFORGE_SOURCE_URL"] = self.source_url
            if self.source_id is not None:
                trackforge["TRACKFORGE_SOURCE_ID"] = self.source_id
            object.__setattr__(self, "trackforge", trackforge)
        for field_name, value in self.mbids.items():
            if getattr(self, field_name) is None:
                object.__setattr__(self, field_name, value)
        if self.source_url is not None:
            object.__setattr__(self, "source", self.source_url)
        if self.catalog_number is not None:
            object.__setattr__(self, "catalog", self.catalog_number)
        object.__setattr__(
            self,
            "mbids",
            {
                field_name: value
                for field_name in _MBID_ALIASES.values()
                if (value := getattr(self, field_name)) is not None
            },
        )
        if is_synced_lyrics(self.lyrics):
            raise DomainValidationError(
                "plain lyrics must not contain LRC timecodes; use the lyrics sidecar",
                context={"field": "lyrics"},
            )

    @classmethod
    def from_resolved(
        cls,
        metadata: ResolvedMetadata,
        *,
        derivative: Sequence[str] = (),
    ) -> TagSet:
        """Project resolved decisions into the canonical tag set."""

        values = metadata.fields

        def text(*names: str) -> str | None:
            for name in names:
                value = values.get(name)
                if isinstance(value, str) and value.strip():
                    return value.strip()
            return None

        def texts(*names: str) -> tuple[str, ...]:
            for name in names:
                value = values.get(name)
                if isinstance(value, str):
                    parts = _text_tuple(value, name)
                    if parts:
                        return parts
                if isinstance(value, (list, tuple)):
                    parts = _text_tuple(value, name)
                    if parts:
                        return parts
            return ()

        trackforge: dict[str, str] = {
            "TRACKFORGE_ITEM_ID": str(metadata.item_id),
            "TRACKFORGE_DECISION_ID": str(metadata.decision_record_id),
        }
        if derivative:
            trackforge["TRACKFORGE_DERIVATIVE"] = ", ".join(
                dict.fromkeys(item.strip() for item in derivative if item.strip())
            )
        source_url = text("source_url", "source")
        source_id = text("source_id")
        if source_url:
            trackforge["TRACKFORGE_SOURCE_URL"] = source_url
        if source_id:
            trackforge["TRACKFORGE_SOURCE_ID"] = source_id
        return cls(
            title=text("title"),
            artists=texts("artist", "artists"),
            albumartist=text("albumartist"),
            album=text("album"),
            date=text("date"),
            originaldate=text("originaldate"),
            originaltitle=text("originaltitle"),
            remixer=text("remixer"),
            originalartist=text("originalartist"),
            qualifier=text("qualifier"),
            artwork=text("artwork"),
            track=_int_value(values.get("track")),
            tracktotal=_int_value(values.get("tracktotal")),
            disc=_int_value(values.get("disc")),
            disctotal=_int_value(values.get("disctotal")),
            genre=texts("genre"),
            bpm=_float_value(values.get("bpm")),
            key=text("key"),
            language=text("language"),
            label=text("label"),
            catalog_number=text("catalog_number"),
            barcode=text("barcode"),
            isrc=text("isrc"),
            compilation=_bool_value(values.get("compilation")),
            comment=text("comment"),
            description=text("description"),
            lyrics=None,
            source_url=source_url,
            source_id=source_id,
            copyright=text("copyright"),
            license=text("license"),
            mb_recording_id=text("mb_recording_id"),
            mb_release_id=text("mb_release_id"),
            mb_release_group_id=text("mb_release_group_id"),
            mb_artist_id=text("mb_artist_id"),
            mb_albumartist_id=text("mb_albumartist_id"),
            acoustid=text("acoustid"),
            trackforge=trackforge,
        )

    def metadata_document(self) -> dict[str, Any]:
        """Return canonical, JSON-compatible metadata for the sidecar."""

        return {
            "title": self.title,
            "artist": list(self.artists),
            "albumartist": self.albumartist,
            "album": self.album,
            "date": self.date,
            "originaldate": self.originaldate,
            "originaltitle": self.originaltitle,
            "remixer": self.remixer,
            "originalartist": self.originalartist,
            "qualifier": self.qualifier,
            "artwork": self.artwork,
            "track": self.track,
            "tracktotal": self.tracktotal,
            "disc": self.disc,
            "disctotal": self.disctotal,
            "genre": list(self.genre),
            "bpm": self.bpm,
            "key": self.key,
            "language": self.language,
            "label": self.label,
            "catalog_number": self.catalog_number,
            "barcode": self.barcode,
            "isrc": self.isrc,
            "compilation": self.compilation,
            "comment": self.comment,
            "description": self.description,
            "lyrics": self.lyrics,
            "source_url": self.source_url,
            "source_id": self.source_id,
            "copyright": self.copyright,
            "license": self.license,
            "mb_recording_id": self.mb_recording_id,
            "mb_release_id": self.mb_release_id,
            "mb_release_group_id": self.mb_release_group_id,
            "mb_artist_id": self.mb_artist_id,
            "mb_albumartist_id": self.mb_albumartist_id,
            "acoustid": self.acoustid,
        }

    def with_lyrics(self, lyrics: str | None) -> TagSet:
        """Return a copy with plain lyrics set or cleared."""

        return TagSet(
            title=self.title,
            artists=self.artists,
            albumartist=self.albumartist,
            album=self.album,
            date=self.date,
            originaldate=self.originaldate,
            originaltitle=self.originaltitle,
            remixer=self.remixer,
            originalartist=self.originalartist,
            qualifier=self.qualifier,
            artwork=self.artwork,
            track=self.track,
            tracktotal=self.tracktotal,
            disc=self.disc,
            disctotal=self.disctotal,
            genre=self.genre,
            bpm=self.bpm,
            key=self.key,
            language=self.language,
            label=self.label,
            catalog_number=self.catalog_number,
            barcode=self.barcode,
            isrc=self.isrc,
            compilation=self.compilation,
            comment=self.comment,
            description=self.description,
            lyrics=lyrics,
            source_url=self.source_url,
            source_id=self.source_id,
            copyright=self.copyright,
            license=self.license,
            mb_recording_id=self.mb_recording_id,
            mb_release_id=self.mb_release_id,
            mb_release_group_id=self.mb_release_group_id,
            mb_artist_id=self.mb_artist_id,
            mb_albumartist_id=self.mb_albumartist_id,
            acoustid=self.acoustid,
            trackforge=dict(self.trackforge),
            source=self.source,
            catalog=self.catalog,
            mbids=dict(self.mbids),
        )


@dataclass(frozen=True, slots=True)
class RenderedAudio:
    """The audio file produced by a renderer before tags are written."""

    path: Path
    format: TargetFormat
    codec: str | None = None
    sample_rate: int | None = None
    channels: int | None = None
    bitrate: int | None = None

    def __post_init__(self) -> None:
        path = Path(self.path)
        if not path.is_absolute():
            path = Path.cwd() / path
        object.__setattr__(self, "path", path)
        object.__setattr__(self, "format", _coerce_target(self.format, "format"))


@dataclass(frozen=True, slots=True)
class TagMismatch:
    """One field that read-back did not confirm."""

    field: str
    expected: object
    actual: object
    detail: str = ""

    def __post_init__(self) -> None:
        if not self.field.strip():
            raise DomainValidationError("tag mismatch field must not be empty")
        object.__setattr__(self, "field", self.field.strip())

    def message(self) -> str:
        detail = f": {self.detail}" if self.detail else ""
        return f"{self.field}: expected {self.expected!r}, got {self.actual!r}{detail}"


@dataclass(frozen=True, slots=True)
class RenderResult:
    """Persisted render metadata plus the concrete workspace bundle."""

    render: Render
    bundle: OutputBundle
    directory: Path
    tagset: TagSet
    warnings: tuple[str, ...] = ()
    created_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        if not isinstance(self.render, Render):
            raise DomainValidationError("render must be a Render")
        if not isinstance(self.bundle, OutputBundle):
            raise DomainValidationError("bundle must be an OutputBundle")
        object.__setattr__(self, "directory", Path(self.directory))
        object.__setattr__(self, "warnings", tuple(self.warnings))
        object.__setattr__(self, "created_at", ensure_datetime(self.created_at, "created_at"))

    @property
    def render_id(self) -> RenderId:
        return self.render.id

    @property
    def bundle_hash(self) -> str:
        return self.bundle.bundle_hash

    @property
    def files(self) -> tuple[str, ...]:
        return self.bundle.files

    @property
    def audio_path(self) -> Path:
        return self.directory / f"track.{extension_for(self.render.target_format)}"


def _int_value(value: FieldValue) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _float_value(value: FieldValue) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _bool_value(value: FieldValue) -> bool | None:
    return value if isinstance(value, bool) else None


__all__ = [
    "COVER_MAX_BYTES",
    "COVER_MAX_DIMENSION",
    "COVER_SIDECAR_NAME",
    "ELRC_SIDECAR_SUFFIX",
    "LRC_SIDECAR_SUFFIX",
    "RENDER_NAMING_TEMPLATE",
    "SIDECAR_NAME",
    "TAG_MAPPING_VERSION",
    "CoverArt",
    "RenderOptions",
    "RenderResult",
    "RenderedAudio",
    "TagMismatch",
    "TagSet",
    "extension_for",
    "is_supported_container",
    "is_synced_lyrics",
    "lyrics_sidecar_name",
    "target_format_for",
]
