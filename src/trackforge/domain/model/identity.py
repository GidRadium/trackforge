"""Lightweight identity types.

In v1 these entities live in claims and the ``identity.hypotheses.v1``
artifact; there are deliberately no normalized tables or relationship graphs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from trackforge.domain.model._validation import (
    Array,
    coerce_enum,
    ensure_bool,
    ensure_float,
    ensure_sequence,
    ensure_str_tuple,
    ensure_string_mapping,
    json_mapping,
    optional_id,
    optional_text,
    require_id,
    require_text,
    validate_confidence,
)
from trackforge.domain.model.ids import (
    ArtistId,
    DerivativeId,
    IdentityHypothesisId,
    ItemId,
    RecordingId,
    ReleaseGroupId,
    ReleaseId,
    WorkId,
)


class ContentKind(StrEnum):
    MUSIC = "music"
    AUDIOBOOK = "audiobook"
    PODCAST = "podcast"
    TALK = "talk"
    SOUND_EFFECT = "sound_effect"
    OTHER = "other"


class Transformation(StrEnum):
    NIGHTCORE = "nightcore"
    SPED_UP = "sped_up"
    SLOWED = "slowed"
    REVERB = "reverb"
    EIGHT_D = "8d"
    REMIX = "remix"
    BOOTLEG = "bootleg"
    MASHUP = "mashup"
    COVER = "cover"
    INSTRUMENTAL = "instrumental"
    ACAPELLA = "acapella"
    KARAOKE = "karaoke"
    RADIO_EDIT = "radio_edit"
    EXTENDED = "extended"
    LIVE = "live"
    ACOUSTIC = "acoustic"
    DEMO = "demo"
    OTHER = "other"


class IdentityTargetType(StrEnum):
    RECORDING = "recording"
    RELEASE = "release"
    RELEASE_GROUP = "release_group"
    WORK = "work"
    DERIVATIVE = "derivative"


class IdentityStatus(StrEnum):
    PROPOSED = "proposed"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    AMBIGUOUS = "ambiguous"


@dataclass(frozen=True, slots=True)
class Work:
    id: WorkId
    title: str
    composers: tuple[str, ...] = ()
    lyricists: tuple[str, ...] = ()
    iswc: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", require_id(self.id, "id"))
        object.__setattr__(self, "title", require_text(self.title, "title"))
        object.__setattr__(self, "composers", ensure_str_tuple(self.composers, "composers"))
        object.__setattr__(self, "lyricists", ensure_str_tuple(self.lyricists, "lyricists"))
        object.__setattr__(self, "iswc", optional_text(self.iswc, "iswc"))


@dataclass(frozen=True, slots=True)
class Recording:
    id: RecordingId
    title: str
    artist_ids: tuple[ArtistId, ...] = ()
    duration_seconds: float | None = None
    isrc: str | None = None
    mbid: str | None = None
    work_id: WorkId | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", require_id(self.id, "id"))
        object.__setattr__(self, "title", require_text(self.title, "title"))
        raw_artist_ids = ensure_sequence(self.artist_ids, "artist_ids")
        object.__setattr__(
            self,
            "artist_ids",
            Array(require_id(item, "artist_ids[]") for item in raw_artist_ids),
        )
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
        object.__setattr__(self, "isrc", optional_text(self.isrc, "isrc"))
        object.__setattr__(self, "mbid", optional_text(self.mbid, "mbid"))
        object.__setattr__(self, "work_id", optional_id(self.work_id, "work_id"))


@dataclass(frozen=True, slots=True)
class ReleaseGroup:
    id: ReleaseGroupId
    title: str
    primary_type: str | None = None
    secondary_types: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", require_id(self.id, "id"))
        object.__setattr__(self, "title", require_text(self.title, "title"))
        object.__setattr__(
            self,
            "primary_type",
            optional_text(self.primary_type, "primary_type"),
        )
        object.__setattr__(
            self,
            "secondary_types",
            ensure_str_tuple(self.secondary_types, "secondary_types"),
        )


@dataclass(frozen=True, slots=True)
class Release:
    id: ReleaseId
    title: str
    release_group_id: ReleaseGroupId | None = None
    date: str | None = None
    label: str | None = None
    catalog_number: str | None = None
    barcode: str | None = None
    country: str | None = None
    artist_ids: tuple[ArtistId, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", require_id(self.id, "id"))
        object.__setattr__(self, "title", require_text(self.title, "title"))
        object.__setattr__(
            self,
            "release_group_id",
            optional_id(self.release_group_id, "release_group_id"),
        )
        object.__setattr__(self, "date", optional_text(self.date, "date"))
        object.__setattr__(self, "label", optional_text(self.label, "label"))
        object.__setattr__(
            self,
            "catalog_number",
            optional_text(self.catalog_number, "catalog_number"),
        )
        object.__setattr__(self, "barcode", optional_text(self.barcode, "barcode"))
        object.__setattr__(self, "country", optional_text(self.country, "country"))
        raw_artist_ids = ensure_sequence(self.artist_ids, "artist_ids")
        object.__setattr__(
            self,
            "artist_ids",
            Array(require_id(item, "artist_ids[]") for item in raw_artist_ids),
        )


@dataclass(frozen=True, slots=True)
class Artist:
    id: ArtistId
    name: str
    sort_name: str | None = None
    mbid: str | None = None
    aliases: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", require_id(self.id, "id"))
        object.__setattr__(self, "name", require_text(self.name, "name"))
        object.__setattr__(self, "sort_name", optional_text(self.sort_name, "sort_name"))
        object.__setattr__(self, "mbid", optional_text(self.mbid, "mbid"))
        object.__setattr__(self, "aliases", ensure_str_tuple(self.aliases, "aliases"))


@dataclass(frozen=True, slots=True)
class Derivative:
    id: DerivativeId
    transformations: tuple[Transformation, ...] = ()
    source_recording_id: RecordingId | None = None
    parameters: dict[str, Any] = field(default_factory=dict)
    derivative_title: str | None = None
    derivative_artist: str | None = None
    is_official: bool | None = None
    evidence: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", require_id(self.id, "id"))
        raw_transformations = ensure_sequence(self.transformations, "transformations")
        transformed = Array(
            coerce_enum(item, Transformation, f"transformations[{index}]")
            for index, item in enumerate(raw_transformations)
        )
        object.__setattr__(self, "transformations", transformed)
        object.__setattr__(
            self,
            "source_recording_id",
            optional_id(self.source_recording_id, "source_recording_id"),
        )
        object.__setattr__(
            self,
            "parameters",
            json_mapping(self.parameters, "parameters"),
        )
        object.__setattr__(
            self,
            "derivative_title",
            optional_text(self.derivative_title, "derivative_title"),
        )
        object.__setattr__(
            self,
            "derivative_artist",
            optional_text(self.derivative_artist, "derivative_artist"),
        )
        if self.is_official is not None:
            object.__setattr__(
                self,
                "is_official",
                ensure_bool(self.is_official, "is_official"),
            )
        object.__setattr__(self, "evidence", ensure_str_tuple(self.evidence, "evidence"))


@dataclass(frozen=True, slots=True)
class IdentityHypothesis:
    id: IdentityHypothesisId
    item_id: ItemId
    target_type: IdentityTargetType
    score: float
    target_id: str | None = None
    external_ids: dict[str, str] = field(default_factory=dict)
    evidence: tuple[str, ...] = ()
    status: IdentityStatus = IdentityStatus.PROPOSED

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", require_id(self.id, "id"))
        object.__setattr__(self, "item_id", require_id(self.item_id, "item_id"))
        object.__setattr__(
            self,
            "target_type",
            coerce_enum(self.target_type, IdentityTargetType, "target_type"),
        )
        object.__setattr__(
            self,
            "score",
            validate_confidence(self.score, "score"),
        )
        object.__setattr__(self, "target_id", optional_id(self.target_id, "target_id"))
        object.__setattr__(
            self,
            "external_ids",
            ensure_string_mapping(self.external_ids, "external_ids"),
        )
        object.__setattr__(self, "evidence", ensure_str_tuple(self.evidence, "evidence"))
        object.__setattr__(
            self,
            "status",
            coerce_enum(self.status, IdentityStatus, "status"),
        )


__all__ = [
    "Artist",
    "ContentKind",
    "Derivative",
    "IdentityHypothesis",
    "IdentityStatus",
    "IdentityTargetType",
    "Recording",
    "Release",
    "ReleaseGroup",
    "Transformation",
    "Work",
]
