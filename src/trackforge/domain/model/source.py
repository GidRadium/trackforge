"""Source references, source candidates, and their format descriptions."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from trackforge.domain.errors import DomainValidationError
from trackforge.domain.model._validation import (
    Array,
    coerce_enum,
    coerce_path,
    ensure_bool,
    ensure_float,
    ensure_int,
    ensure_sequence,
    json_mapping,
    optional_text,
    require_id,
    require_text,
)
from trackforge.domain.model.ids import CandidateId


class CollectionKind(StrEnum):
    PLAYLIST = "playlist"
    ALBUM = "album"
    CHANNEL = "channel"
    FOLDER = "folder"
    OTHER = "other"


@dataclass(frozen=True, slots=True)
class LocalPath:
    """A local file or folder reference.  Existence is not checked here."""

    path: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", coerce_path(self.path, "path"))


@dataclass(frozen=True, slots=True)
class Url:
    """A remote source URL, optionally with a resolver hint."""

    url: str
    site_hint: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "url", require_text(self.url, "url"))
        object.__setattr__(self, "site_hint", optional_text(self.site_hint, "site_hint"))


@dataclass(frozen=True, slots=True)
class TextQuery:
    """A free-text search input for sources that support search."""

    text: str
    locale: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "text", require_text(self.text, "text"))
        object.__setattr__(self, "locale", optional_text(self.locale, "locale"))


@dataclass(frozen=True, slots=True)
class CollectionRef:
    """A reference to a playlist, album, channel, or folder."""

    kind: CollectionKind
    source: LocalPath | Url | TextQuery | CollectionRef
    external_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "kind", coerce_enum(self.kind, CollectionKind, "kind"))
        if not isinstance(self.source, (LocalPath, Url, TextQuery, CollectionRef)):
            raise DomainValidationError("CollectionRef.source must be a SourceRef")
        object.__setattr__(
            self,
            "external_id",
            optional_text(self.external_id, "external_id"),
        )


SourceRef = LocalPath | Url | TextQuery | CollectionRef


@dataclass(frozen=True, slots=True)
class Format:
    """A downloadable format advertised by a source."""

    codec: str | None = None
    bitrate: int | None = None
    sample_rate: int | None = None
    channels: int | None = None
    language: str | None = None
    size: int | None = None
    url: str | None = None
    drm: bool = False
    container: str | None = None
    format_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "codec", optional_text(self.codec, "codec"))
        if self.bitrate is not None:
            object.__setattr__(
                self,
                "bitrate",
                ensure_int(self.bitrate, "bitrate", minimum=0),
            )
        if self.sample_rate is not None:
            object.__setattr__(
                self,
                "sample_rate",
                ensure_int(self.sample_rate, "sample_rate", minimum=1),
            )
        if self.channels is not None:
            object.__setattr__(
                self,
                "channels",
                ensure_int(self.channels, "channels", minimum=1),
            )
        object.__setattr__(self, "language", optional_text(self.language, "language"))
        if self.size is not None:
            object.__setattr__(self, "size", ensure_int(self.size, "size", minimum=0))
        object.__setattr__(self, "url", optional_text(self.url, "url"))
        object.__setattr__(self, "drm", ensure_bool(self.drm, "drm"))
        object.__setattr__(self, "container", optional_text(self.container, "container"))
        object.__setattr__(
            self,
            "format_id",
            optional_text(self.format_id, "format_id"),
        )

    @property
    def size_bytes(self) -> int | None:
        """Alias matching the naming used by media objects."""

        return self.size


@dataclass(frozen=True, slots=True)
class Thumbnail:
    """A thumbnail candidate returned by a source."""

    url: str
    width: int | None = None
    height: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "url", require_text(self.url, "url"))
        if self.width is not None:
            object.__setattr__(
                self,
                "width",
                ensure_int(self.width, "width", minimum=1),
            )
        if self.height is not None:
            object.__setattr__(
                self,
                "height",
                ensure_int(self.height, "height", minimum=1),
            )


@dataclass(frozen=True, slots=True)
class Candidate:
    """A possible media item offered by a source before acquisition."""

    id: CandidateId
    source_ref: SourceRef
    external_id: str
    title: str
    uploader: str | None = None
    duration_seconds: float | None = None
    formats: tuple[Format, ...] = ()
    thumbnails: tuple[Thumbnail, ...] = ()
    extra: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", require_id(self.id, "id"))
        if not isinstance(self.source_ref, (LocalPath, Url, TextQuery, CollectionRef)):
            raise DomainValidationError("Candidate.source_ref must be a SourceRef")
        object.__setattr__(
            self,
            "external_id",
            require_text(self.external_id, "external_id"),
        )
        object.__setattr__(self, "title", require_text(self.title, "title"))
        object.__setattr__(self, "uploader", optional_text(self.uploader, "uploader"))
        if self.duration_seconds is not None:
            object.__setattr__(
                self,
                "duration_seconds",
                ensure_float(
                    self.duration_seconds,
                    "duration_seconds",
                    minimum=0.0,
                ),
            )

        raw_formats = ensure_sequence(self.formats, "formats")
        for index, item in enumerate(raw_formats):
            if not isinstance(item, Format):
                raise DomainValidationError(f"formats[{index}] must be a Format")
        object.__setattr__(self, "formats", Array(raw_formats))

        raw_thumbnails = ensure_sequence(self.thumbnails, "thumbnails")
        for index, item in enumerate(raw_thumbnails):
            if not isinstance(item, Thumbnail):
                raise DomainValidationError(f"thumbnails[{index}] must be a Thumbnail")
        object.__setattr__(self, "thumbnails", Array(raw_thumbnails))
        object.__setattr__(self, "extra", json_mapping(self.extra, "extra"))


__all__ = [
    "Candidate",
    "CollectionKind",
    "CollectionRef",
    "Format",
    "LocalPath",
    "SourceRef",
    "TextQuery",
    "Thumbnail",
    "Url",
]
