"""Capability descriptions and write-policy value objects.

Capabilities answer a question the normalized model cannot: *can this
container store this field, and how?*  They are intentionally data, not
format-specific logic, so the facade and the CLI can reason about loss before
touching a file.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum

from .formats import ContainerFormat, TagSystem

CORE_FIELDS: tuple[str, ...] = (
    "title",
    "title_sort",
    "artist",
    "artists",
    "artist_sort",
    "date",
    "original_date",
    "release_date",
    "mood",
    "genre",
    "comment",
    "composer",
    "lyricist",
    "lyrics",
    "producer",
    "copyright",
    "language",
    "grouping",
    "musicbrainz_ids",
)

AUDIO_FIELDS: tuple[str, ...] = (
    "album",
    "album_sort",
    "album_artist",
    "album_artist_sort",
    "track_number",
    "track_total",
    "disc_number",
    "disc_total",
    "compilation",
    "label",
    "isrc",
    "bpm",
    "key",
    "replaygain",
)

VIDEO_FIELDS: tuple[str, ...] = (
    "show",
    "season",
    "episode",
    "episode_id",
    "network",
    "director",
    "writer",
    "synopsis",
    "rating",
    "keywords",
    "location",
)


class UnsupportedPolicy(StrEnum):
    """What to do when a requested field cannot be stored."""

    ERROR = "error"
    SKIP = "skip"
    COERCE = "coerce"


@dataclass(frozen=True, slots=True)
class FieldCapability:
    """How one normalized field maps into one container."""

    supported: bool = True
    multi: bool = False
    value_type: type = str
    max_bytes: int | None = None
    encoding: str = "utf-8"
    native_key: str | None = None


@dataclass(frozen=True, slots=True)
class Capabilities:
    """Capabilities of one container/tag-system pair."""

    container: ContainerFormat
    tag_system: TagSystem
    fields: Mapping[str, FieldCapability] = field(default_factory=dict)
    arbitrary_keys: bool = False
    artwork: bool = False
    writable: bool = False

    def supports(self, field_name: str) -> bool:
        capability = self.fields.get(field_name)
        return capability is not None and capability.supported

    def field(self, field_name: str) -> FieldCapability | None:
        return self.fields.get(field_name)


@dataclass(frozen=True, slots=True)
class WritePolicy:
    """Policy for a write operation.

    ``SKIP`` is the friendly default: write what fits and return the rest in
    :class:`WriteReport`.  ``ERROR`` makes loss explicit and refuses to write.
    ``remove_missing`` treats ``None``/empty fields as deletions; it is off by
    default so a partially populated object cannot erase a file's tags.
    """

    on_unsupported: UnsupportedPolicy = UnsupportedPolicy.SKIP
    write_legacy: bool = False
    preserve_unknown: bool = True
    verify: bool = True
    remove_missing: bool = False
    atomic: bool = True
    id3_version: int = 4
    write_extra: bool = False

    def __post_init__(self) -> None:
        if self.id3_version not in (3, 4):
            raise ValueError("id3_version must be 3 or 4")


@dataclass(slots=True)
class WriteReport:
    """What actually happened during a write."""

    backend: str = ""
    container: ContainerFormat = ContainerFormat.UNKNOWN
    written: list[str] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)
    coerced: list[tuple[str, str]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    bytes_before: int = 0
    bytes_after: int = 0

    @property
    def changed(self) -> bool:
        return self.bytes_before != self.bytes_after

    @property
    def lossy(self) -> bool:
        return bool(self.skipped or self.coerced)


def standard_capabilities(
    container: ContainerFormat,
    tag_system: TagSystem,
    *,
    audio: bool = True,
    video: bool = False,
    native_keys: Mapping[str, str] | None = None,
    arbitrary_keys: bool = True,
    artwork: bool = True,
    writable: bool = False,
    unsupported: Iterable[str] = (),
) -> Capabilities:
    """Build a capability table for a container and its field groups."""
    keys = native_keys or {}
    names = list(CORE_FIELDS)
    if audio:
        names.extend(AUDIO_FIELDS)
    if video:
        names.extend(VIDEO_FIELDS)

    unsupported_names = set(unsupported)
    fields = {
        name: FieldCapability(
            supported=name not in unsupported_names,
            native_key=keys.get(name),
        )
        for name in names
    }
    return Capabilities(
        container=container,
        tag_system=tag_system,
        fields=fields,
        arbitrary_keys=arbitrary_keys,
        artwork=artwork,
        writable=writable,
    )
