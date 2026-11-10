"""Normalized acquisition value objects.

These objects are deliberately independent of yt-dlp and of any concrete
source.  Adapters translate source-specific payloads into this model; the
rest of the application only sees normalized data.  Source-specific fields
belong in ``extras`` instead of being promoted to the public model.
"""

# pyright: standard

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

from ..metadata.model import PartialDate

if TYPE_CHECKING:
    from .serialization import SourceInfoDict

# The public inspect JSON contract.  Bump this only together with a documented
# schema change; internal/model additions that are not serialized do not need
# a bump, but public fields do.
SOURCE_INFO_SCHEMA_VERSION = 1


class InputKind(StrEnum):
    """How a raw user input was classified."""

    URL = "url"
    TEXT = "text"
    LOCAL_FILE = "local_file"
    LOCAL_DIR = "local_dir"


class SourceKind(StrEnum):
    """Shape of a resolved source."""

    SINGLE = "single"
    POST = "post"
    COLLECTION = "collection"


class UrlHint(StrEnum):
    """Offline guess about a URL; ``get_info`` has the final word."""

    SINGLE = "single"
    POST = "post"
    COLLECTION = "collection"
    UNKNOWN = "unknown"


class LiveStatus(StrEnum):
    """Live-stream state reported by a source."""

    LIVE = "is_live"
    UPCOMING = "is_upcoming"
    WAS_LIVE = "was_live"
    POST_LIVE = "post_live"
    NOT_LIVE = "not_live"
    UNKNOWN = "unknown"


class MediaKind(StrEnum):
    """What kind of media an item or variant contains."""

    AUDIO = "audio"
    VIDEO = "video"
    AUDIO_VIDEO = "audio_video"
    IMAGE = "image"
    ANIMATED_IMAGE = "animated_image"
    DOCUMENT = "document"
    UNKNOWN = "unknown"


class StreamRole(StrEnum):
    """Role of one stream inside a download variant."""

    VIDEO = "video"
    AUDIO = "audio"
    IMAGE = "image"
    DOCUMENT = "document"


class MediaUse(StrEnum):
    """A user-facing reason to pick a variant."""

    AUDIO = "audio"
    VIDEO = "video"
    IMAGE = "image"
    DOCUMENT = "document"


class ResolutionState(StrEnum):
    """How complete the metadata for an item is."""

    FULL = "full"
    FLAT = "flat"
    UNRESOLVED = "unresolved"


@dataclass(frozen=True, slots=True)
class Input:
    """A raw command-line input after local classification."""

    raw: str
    kind: InputKind
    url: str | None = None
    query: str | None = None
    paths: tuple[Path, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.kind, InputKind):
            object.__setattr__(self, "kind", InputKind(self.kind))


@dataclass(frozen=True, slots=True)
class SourceRef:
    """A source reference that can be passed to ``download()``."""

    source: str
    url: str
    id: str | None = None


@dataclass(frozen=True, slots=True)
class Thumbnail:
    """A thumbnail URL plus the metadata needed to choose or fetch it."""

    url: str
    width: int | None = None
    height: int | None = None
    ext: str | None = None
    mime: str | None = None
    filesize: int | None = None
    preference: int = 0
    source: str | None = None
    http_headers: Mapping[str, str] = field(default_factory=dict, repr=False)


@dataclass(frozen=True, slots=True)
class ThumbnailAsset:
    """Thumbnail bytes, ready for a preview or a chat bot."""

    data: bytes
    mime: str
    width: int | None = None
    height: int | None = None


@dataclass(frozen=True, slots=True)
class StreamInfo:
    """One audio/video/image/document stream in a variant."""

    role: StreamRole
    format_id: str | None = None
    codec: str | None = None
    ext: str | None = None
    url: str | None = field(default=None, repr=False)
    width: int | None = None
    height: int | None = None
    fps: float | None = None
    bitrate_kbps: float | None = None
    sample_rate: int | None = None
    channels: int | None = None
    language: str | None = None
    language_preference: float | None = None
    dynamic_range: str | None = None
    filesize: int | None = None
    filesize_approx: int | None = None
    protocol: str | None = None
    drm: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.role, StreamRole):
            object.__setattr__(self, "role", StreamRole(self.role))


@dataclass(frozen=True, slots=True)
class DownloadVariant:
    """One output file offered to the user."""

    id: str
    label: str
    kind: MediaKind
    container: str | None = None
    streams: tuple[StreamInfo, ...] = ()
    selector: str | None = None
    filesize: int | None = None
    filesize_approx: int | None = None
    requires_merge: bool = False
    requires_ffmpeg: bool = False
    downloadable: bool = True
    unavailable_reason: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.kind, MediaKind):
            object.__setattr__(self, "kind", MediaKind(self.kind))

    @property
    def size_label(self) -> str:
        size = self.filesize or self.filesize_approx
        if size is None:
            return "?"
        prefix = "" if self.filesize else "~"
        return f"{prefix}{size / 1_000_000:.1f} MB"


@dataclass(frozen=True, slots=True)
class Recommendations:
    """Variant ids the loader considers best for each use."""

    audio: str | None = None
    video: str | None = None
    image: str | None = None
    document: str | None = None

    def for_use(self, use: MediaUse | str) -> str | None:
        selected = use if isinstance(use, MediaUse) else MediaUse(use)
        return getattr(self, selected.value)


@dataclass(frozen=True, slots=True)
class SubtitleTrack:
    """One subtitle/caption track exposed by a source.

    YouTube returns several formats per language (vtt, ttml, json3, srv*);
    all of them are kept.  ``is_automatic`` distinguishes generated captions
    from uploader-provided subtitles.
    """

    language: str
    url: str | None = field(default=None, repr=False)
    ext: str | None = None
    name: str | None = None
    mime: str | None = None
    is_automatic: bool = False
    data: str | None = field(default=None, repr=False)
    http_headers: Mapping[str, str] = field(default_factory=dict, repr=False)


@dataclass(frozen=True, slots=True)
class MediaItem:
    """One downloadable media item inside a source."""

    id: str
    title: str
    suggested_stem: str | None = None
    kind: MediaKind = MediaKind.UNKNOWN
    creator: str | None = None
    artists: tuple[str, ...] = ()
    album: str | None = None
    album_artists: tuple[str, ...] = ()
    duration: float | None = None
    live_status: LiveStatus = LiveStatus.UNKNOWN
    release_date: PartialDate | None = None
    page_url: str | None = None
    thumbnails: tuple[Thumbnail, ...] = ()
    thumbnail: Thumbnail | None = None
    subtitles: tuple[SubtitleTrack, ...] = ()
    variants: tuple[DownloadVariant, ...] = ()
    recommendations: Recommendations = field(default_factory=Recommendations)
    resolution: ResolutionState = ResolutionState.FULL
    position: int | None = None
    group_id: str | None = None
    extras: Mapping[str, object] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.kind, MediaKind):
            object.__setattr__(self, "kind", MediaKind(self.kind))
        if not isinstance(self.live_status, LiveStatus):
            object.__setattr__(self, "live_status", LiveStatus(self.live_status))
        if not isinstance(self.resolution, ResolutionState):
            object.__setattr__(
                self,
                "resolution",
                ResolutionState(self.resolution),
            )


@dataclass(frozen=True, slots=True)
class SourceInfo:
    """A resolved source: a single item, a post or a collection."""

    SCHEMA_VERSION: ClassVar[int] = SOURCE_INFO_SCHEMA_VERSION

    source: str
    url: str
    kind: SourceKind
    id: str | None = None
    title: str | None = None
    creator: str | None = None
    description: str | None = None
    thumbnails: tuple[Thumbnail, ...] = ()
    items: tuple[MediaItem, ...] = ()
    warnings: tuple[str, ...] = ()
    thumbnail: Thumbnail | None = None
    publisher: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.kind, SourceKind):
            object.__setattr__(self, "kind", SourceKind(self.kind))

    @property
    def schema_version(self) -> int:
        """Version of the public ``inspect`` JSON contract."""
        return SOURCE_INFO_SCHEMA_VERSION

    def to_dict(self) -> SourceInfoDict:
        """Return the frozen schema-version-1 representation."""
        from .serialization import source_info_to_dict

        return source_info_to_dict(self)

    def to_json(self, *, indent: int | None = 2) -> str:
        """Return the frozen schema-version-1 representation as JSON text."""
        from .serialization import source_info_to_json

        return source_info_to_json(self, indent=indent)

    def ref(self) -> SourceRef:
        return SourceRef(self.source, self.url, self.id)

    @property
    def recommendations(self) -> Recommendations:
        return self.items[0].recommendations if self.items else Recommendations()


@dataclass(frozen=True, slots=True)
class SearchCandidate:
    """A light search result; formats are resolved only after selection."""

    source: str
    id: str
    url: str
    title: str
    creator: str | None = None
    duration: float | None = None
    thumbnails: tuple[Thumbnail, ...] = ()
    thumbnail: Thumbnail | None = None
    published_at: PartialDate | None = None
    score: float = 0.0


class CollisionPolicy(StrEnum):
    """What to do when the target path already exists."""

    ERROR = "error"
    SKIP = "skip"
    SUBFOLDER = "subfolder"
    OVERWRITE = "overwrite"


@dataclass(frozen=True, slots=True)
class ItemSelection:
    """One item/variant chosen by the user for download."""

    item_id: str
    variant_id: str


@dataclass(frozen=True, slots=True)
class DownloadOptions:
    """How to download the selected items."""

    target_dir: Path
    staging_name: str | None = None
    collision: CollisionPolicy = CollisionPolicy.SUBFOLDER
    write_thumbnail: bool = True
    write_info_json: bool = False
    subtitles: tuple[str, ...] = ()
    subtitles_to_lrc: bool = False
    write_lyrics: bool = True
    keep_temp: bool = False
    audio_only: bool = False
    container: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "target_dir", Path(self.target_dir).expanduser())
        if not isinstance(self.collision, CollisionPolicy):
            object.__setattr__(
                self,
                "collision",
                CollisionPolicy(self.collision),
            )


@dataclass(frozen=True, slots=True)
class DownloadRequest:
    """A snapshot of the resolved source plus the user's selections."""

    source_info: SourceInfo
    selections: tuple[ItemSelection, ...]
    options: DownloadOptions

    @property
    def source(self) -> SourceRef:
        return self.source_info.ref()


@dataclass(frozen=True, slots=True)
class DownloadedFile:
    """One successfully downloaded media file and its sidecars."""

    item_id: str
    path: Path
    item: MediaItem
    sidecars: tuple[Path, ...] = ()
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class DownloadFailure:
    """A per-item download failure inside a multi-item request."""

    item_id: str
    message: str
    retryable: bool = False


@dataclass(frozen=True, slots=True)
class DownloadResult:
    """The aggregate result of a download request."""

    files: tuple[DownloadedFile, ...]
    failures: tuple[DownloadFailure, ...] = ()
    warnings: tuple[str, ...] = ()
