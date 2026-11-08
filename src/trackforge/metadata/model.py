"""Normalized metadata value objects.

The model is deliberately independent of file formats and third-party
libraries.  Adapters translate container-specific tags into these objects;
the public facade exposes them without leaking format details.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum


class MediaKind(StrEnum):
    """What kind of media streams a document contains."""

    AUDIO = "audio"
    VIDEO = "video"
    AUDIO_VIDEO = "audio_video"
    UNKNOWN = "unknown"


class ArtworkKind(StrEnum):
    """Semantic role of an embedded image."""

    FRONT = "front"
    BACK = "back"
    ARTIST = "artist"
    LOGO = "logo"
    OTHER = "other"


@dataclass(frozen=True, slots=True)
class PartialDate:
    """A date that may be known only partially.

    Music metadata frequently contains only a year, or a year and a month,
    so converting everything to :class:`datetime.date` would lose information.
    """

    year: int
    month: int | None = None
    day: int | None = None

    def __post_init__(self) -> None:
        if not 1 <= self.year <= 9999:
            raise ValueError(f"year must be in 1..9999, got {self.year}")
        if self.month is None:
            if self.day is not None:
                raise ValueError("day cannot be set without month")
            return
        if not 1 <= self.month <= 12:
            raise ValueError(f"month must be in 1..12, got {self.month}")
        if self.day is None:
            return
        try:
            date(self.year, self.month, self.day)
        except ValueError as exc:
            raise ValueError(f"invalid date: {exc}") from exc

    def __str__(self) -> str:
        if self.month is None:
            return f"{self.year:04d}"
        if self.day is None:
            return f"{self.year:04d}-{self.month:02d}"
        return f"{self.year:04d}-{self.month:02d}-{self.day:02d}"


@dataclass(frozen=True, slots=True)
class Lyrics:
    """Lyrics with the language and synchronization flag that tags carry."""

    text: str
    language: str | None = None
    synced: bool = False


@dataclass(frozen=True, slots=True)
class Artwork:
    """An embedded image.

    ``kind`` accepts :class:`ArtworkKind` or its string value, so callers do
    not have to import the enum for the common case.
    """

    data: bytes
    mime: str
    kind: ArtworkKind | str = ArtworkKind.FRONT
    description: str | None = None
    width: int | None = None
    height: int | None = None

    def __post_init__(self) -> None:
        if not self.data:
            raise ValueError("artwork data cannot be empty")
        if not self.mime:
            raise ValueError("artwork MIME type cannot be empty")
        if isinstance(self.kind, str) and not isinstance(self.kind, ArtworkKind):
            object.__setattr__(self, "kind", ArtworkKind(self.kind))
        if self.width is not None and self.width < 1:
            raise ValueError("artwork width must be positive")
        if self.height is not None and self.height < 1:
            raise ValueError("artwork height must be positive")


@dataclass(slots=True)
class ArtworkCollection:
    """A small mutable sequence of artwork images.

    The facade returns one of these and writes all images on ``save()``.
    """

    _items: list[Artwork] = field(default_factory=list)

    def __iter__(self):
        return iter(self._items)

    def __len__(self) -> int:
        return len(self._items)

    def __bool__(self) -> bool:
        return bool(self._items)

    def __getitem__(self, index: int | slice) -> Artwork | list[Artwork]:
        return self._items[index]

    def add(self, artwork: Artwork) -> None:
        self._items.append(artwork)

    def extend(self, artworks: list[Artwork]) -> None:
        self._items.extend(artworks)

    @property
    def front(self) -> Artwork | None:
        """Return the first front cover, if any."""
        return next(
            (item for item in self._items if item.kind is ArtworkKind.FRONT),
            None,
        )

    def to_list(self) -> list[Artwork]:
        return list(self._items)


@dataclass(slots=True)
class CommonMetadata:
    """Fields shared by audio and video documents."""

    title: str | None = None
    title_sort: str | None = None
    artist: str | None = None
    artists: list[str] = field(default_factory=list)
    artist_sort: str | None = None
    date: PartialDate | None = None
    genre: list[str] = field(default_factory=list)
    comment: str | None = None
    composer: list[str] = field(default_factory=list)
    lyricist: list[str] = field(default_factory=list)
    conductor: list[str] = field(default_factory=list)
    lyrics: Lyrics | None = None
    producer: list[str] = field(default_factory=list)
    copyright: str | None = None
    language: str | None = None
    grouping: str | None = None
    musicbrainz_ids: dict[str, str] = field(default_factory=dict)
    extra: dict[str, list[str]] = field(default_factory=dict)


@dataclass(slots=True)
class AudioMetadata:
    """Fields that only make sense for an audio track."""

    album: str | None = None
    album_sort: str | None = None
    album_artist: str | None = None
    album_artist_sort: str | None = None
    track_number: int | None = None
    track_total: int | None = None
    disc_number: int | None = None
    disc_total: int | None = None
    compilation: bool | None = None
    label: str | None = None
    isrc: str | None = None
    bpm: int | None = None
    key: str | None = None
    replaygain: dict[str, float] = field(default_factory=dict)


@dataclass(slots=True)
class VideoMetadata:
    """Fields that only make sense for a video document."""

    show: str | None = None
    season: int | None = None
    episode: int | None = None
    episode_id: str | None = None
    network: str | None = None
    director: list[str] = field(default_factory=list)
    writer: list[str] = field(default_factory=list)
    synopsis: str | None = None
    rating: str | None = None
    keywords: list[str] = field(default_factory=list)
    location: str | None = None


@dataclass(slots=True)
class MediaMetadata(CommonMetadata):
    """Common fields plus optional audio and video facets.

    A file may contain both facets (for example an MP4 with a video stream and
    an audio track), so both are optional rather than mutually exclusive.
    """

    audio: AudioMetadata | None = None
    video: VideoMetadata | None = None

    @property
    def kind(self) -> MediaKind:
        if self.audio is not None and self.video is not None:
            return MediaKind.AUDIO_VIDEO
        if self.audio is not None:
            return MediaKind.AUDIO
        if self.video is not None:
            return MediaKind.VIDEO
        return MediaKind.UNKNOWN
