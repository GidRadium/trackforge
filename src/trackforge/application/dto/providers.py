"""Schemas for external metadata observations.

The payloads in this module are deliberately small, normalized views of
provider responses.  Raw HTTP documents are not part of the durable artifact
format: providers keep only the fields the rest of the pipeline may use, and
every identifier is still validated again before it becomes a claim.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol, cast, runtime_checkable

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator

from trackforge.domain.errors import DomainValidationError


class _Payload(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


class FingerprintTool(_Payload):
    """The external tool that produced an audio fingerprint."""

    id: str = Field(min_length=1)
    version: str = Field(min_length=1)


class AudioFingerprintPayload(_Payload):
    """Schema for ``audio.fingerprint.v1``."""

    fingerprint: str = Field(min_length=1)
    duration_seconds: float = Field(ge=0.0)
    tool: FingerprintTool


class AcoustIdCandidate(_Payload):
    """One AcoustID candidate recording."""

    acoustid_id: str = Field(min_length=1)
    recording_mbids: list[str] = Field(default_factory=list)
    score: float = Field(ge=0.0, le=1.0)
    title: str | None = None
    artist: str | None = None


class AcoustIdLookupPayload(_Payload):
    """Schema for ``acoustid.lookup.v1``."""

    candidates: list[AcoustIdCandidate] = Field(default_factory=list)


class MusicBrainzArtist(_Payload):
    """An artist credit embedded in a recording or release."""

    mbid: str | None = None
    name: str = Field(min_length=1)
    sort_name: str | None = None
    join_phrase: str | None = None
    role: str | None = None


class MusicBrainzReleaseRef(_Payload):
    """A compact release reference attached to a recording."""

    mbid: str | None = None
    title: str = Field(min_length=1)
    date: str | None = None
    country: str | None = None
    label: str | None = None
    barcode: str | None = None
    release_group: str | None = None
    release_group_mbid: str | None = None
    status: str | None = None
    track_count: int | None = Field(default=None, ge=0)


class MusicBrainzRelationship(_Payload):
    """A normalized MusicBrainz recording relationship.

    Relationships are evidence, not truth.  ``official`` is only set when the
    provider explicitly says so; an absent value stays ``None``.
    """

    type: str = Field(min_length=1)
    direction: str | None = None
    target_type: str | None = None
    target_mbid: str | None = None
    target_title: str | None = None
    official: bool | None = None


class MusicBrainzRecording(_Payload):
    """A normalized MusicBrainz recording."""

    mbid: str = Field(min_length=1)
    title: str = Field(min_length=1)
    artists: list[MusicBrainzArtist] = Field(default_factory=list)
    duration: float | None = Field(
        default=None,
        ge=0.0,
        validation_alias=AliasChoices("duration", "duration_seconds"),
    )
    isrcs: list[str] = Field(default_factory=list)
    bpm: float | None = Field(default=None, gt=0.0)
    releases: list[MusicBrainzReleaseRef] = Field(default_factory=list)
    genres: list[str] = Field(default_factory=list)
    relationships: list[MusicBrainzRelationship] = Field(default_factory=list)
    score: float | None = Field(default=None, ge=0.0, le=1.0)

    @property
    def duration_seconds(self) -> float | None:
        return self.duration


class MusicBrainzRelease(_Payload):
    """A normalized MusicBrainz release."""

    mbid: str = Field(min_length=1)
    title: str = Field(min_length=1)
    date: str | None = None
    country: str | None = None
    label: str | None = None
    barcode: str | None = None
    release_group: str | None = None
    release_group_mbid: str | None = None
    artists: list[MusicBrainzArtist] = Field(default_factory=list)
    recordings: list[MusicBrainzRecording] = Field(default_factory=list)
    track_count: int | None = Field(default=None, ge=0)
    status: str | None = None


class MusicBrainzArtistResult(_Payload):
    """A normalized standalone artist search result."""

    mbid: str = Field(min_length=1)
    name: str = Field(min_length=1)
    sort_name: str | None = None
    score: float | None = Field(default=None, ge=0.0, le=1.0)


class MusicBrainzQuery(_Payload):
    """The normalized query that produced a MusicBrainz observation."""

    title: str | None = None
    artist: str | None = None
    album: str | None = None
    duration: float | None = Field(default=None, ge=0.0)
    recording_mbid: str | None = None
    release_mbid: str | None = None
    release_group_mbid: str | None = None
    entity: str | None = None


class MusicBrainzSearchPayload(_Payload):
    """Schema for ``musicbrainz.search.v1``."""

    query: MusicBrainzQuery = Field(default_factory=MusicBrainzQuery)
    recordings: list[MusicBrainzRecording] = Field(default_factory=list)
    releases: list[MusicBrainzRelease] = Field(default_factory=list)
    artists: list[MusicBrainzArtistResult] = Field(default_factory=list)
    raw_ref: str | None = None


class CoverCandidate(_Payload):
    """One cover-art candidate from CAA, iTunes, Deezer, or the source."""

    source: str = Field(min_length=1)
    url: str | None = None
    release_mbid: str | None = None
    mime: str | None = None
    width: int | None = Field(default=None, ge=0)
    height: int | None = Field(default=None, ge=0)
    size_bytes: int | None = Field(default=None, ge=0)
    score: float = Field(default=0.0, ge=0.0, le=1.0)
    artifact_ref: str | None = None

    @property
    def resolution(self) -> int:
        return min(self.width or 0, self.height or 0)


class CoverCandidatesPayload(_Payload):
    """Schema for ``cover.candidates.v1``."""

    candidates: list[CoverCandidate] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class LyricsCandidate(_Payload):
    """One plain/LRC lyrics candidate."""

    source: str = Field(min_length=1)
    title: str = Field(min_length=1)
    artist: str = Field(min_length=1)
    album: str | None = None
    duration: float | None = Field(default=None, ge=0.0)
    plain: str | None = None
    lrc: str | None = None
    language: str | None = None
    has_timing: bool = False
    score: float = Field(default=0.0, ge=0.0, le=1.0)

    @field_validator("has_timing")
    @classmethod
    def _timing_requires_text(cls, value: bool, info: Any) -> bool:
        if value and not str(info.data.get("lrc") or "").strip():
            raise ValueError("has_timing requires lrc text")
        return bool(value)


class LyricsCandidatesPayload(_Payload):
    """Schema for ``lyrics.candidates.v1``."""

    candidates: list[LyricsCandidate] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


@dataclass(frozen=True, slots=True)
class MetadataQuery:
    """A provider-neutral metadata query.

    Providers accept this object or a plain mapping with the same keys.  The
    fingerprint fields are only meaningful for AcoustID; identifier fields are
    only meaningful for lookup-style calls.
    """

    title: str | None = None
    artist: str | None = None
    album: str | None = None
    duration: float | None = None
    fingerprint: str | None = None
    acoustid_id: str | None = None
    recording_mbid: str | None = None
    release_mbid: str | None = None
    release_group_mbid: str | None = None
    artist_mbid: str | None = None
    entity: str | None = None
    limit: int = 8
    source_url: str | None = None

    def __post_init__(self) -> None:
        if self.duration is not None and self.duration < 0:
            raise DomainValidationError("duration must not be negative")
        if self.limit < 1 or self.limit > 100:
            raise DomainValidationError("limit must be between 1 and 100")

    @classmethod
    def coerce(cls, value: MetadataQuery | Mapping[str, Any] | None) -> MetadataQuery:
        """Normalize a provider query from an object or mapping."""

        if value is None:
            return cls()
        if isinstance(value, cls):
            return value
        if isinstance(value, BaseModel):
            value = value.model_dump(exclude_none=True)
        if not isinstance(value, Mapping):
            raise DomainValidationError("metadata query must be a mapping or MetadataQuery")
        allowed = set(cls.__dataclass_fields__)
        aliases = {
            "track_name": "title",
            "track": "title",
            "artist_name": "artist",
            "album_name": "album",
            "duration_seconds": "duration",
            "release_group": "release_group_mbid",
            "release_group_id": "release_group_mbid",
            "recording": "recording_mbid",
            "recording_id": "recording_mbid",
            "release": "release_mbid",
            "release_id": "release_mbid",
            "artist_id": "artist_mbid",
        }
        normalized: dict[str, Any] = {}
        unknown: list[str] = []
        for raw_key, item in value.items():
            key = str(raw_key)
            key = aliases.get(key, key)
            if key not in allowed:
                unknown.append(str(raw_key))
                continue
            normalized[key] = item
        if unknown:
            raise DomainValidationError(
                "unknown metadata query field",
                context={"fields": sorted(unknown)},
            )
        return cls(**normalized)


@dataclass(frozen=True, slots=True)
class ProviderObservation:
    """The normalized result of one external provider call."""

    type: str
    payload: dict[str, Any]
    source: str
    provider_version: str
    warnings: tuple[str, ...] = ()
    expires_at: datetime | None = None
    raw: Mapping[str, Any] | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if not self.type.strip():
            raise DomainValidationError("observation type must not be empty")
        if not self.source.strip():
            raise DomainValidationError("observation source must not be empty")
        if not self.provider_version.strip():
            raise DomainValidationError("provider version must not be empty")
        object.__setattr__(self, "payload", dict(self.payload))
        object.__setattr__(self, "warnings", tuple(self.warnings))

    @property
    def artifact_type(self) -> str:
        """The artifact type this observation materializes as."""

        return self.type

    @property
    def schema_version(self) -> int:
        """The trailing ``vN`` schema version from :attr:`type`."""

        _, _, version = self.type.rpartition(".v")
        try:
            return int(version)
        except ValueError:
            return 1

    def model(self) -> Any:
        """Validate and return the payload model for this observation type."""

        model_type = cast(
            type[BaseModel] | None,
            {
                "audio.fingerprint.v1": AudioFingerprintPayload,
                "acoustid.lookup.v1": AcoustIdLookupPayload,
                "musicbrainz.search.v1": MusicBrainzSearchPayload,
                "cover.candidates.v1": CoverCandidatesPayload,
                "lyrics.candidates.v1": LyricsCandidatesPayload,
            }.get(self.type),
        )
        if model_type is None:
            return None
        return model_type.model_validate(self.payload)


def ttl_expiry(ttl: timedelta, *, now: datetime | None = None) -> datetime:
    """Return a timezone-aware expiration timestamp."""

    current = now or datetime.now(UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    return current.astimezone(UTC) + ttl


@runtime_checkable
class MetadataProvider(Protocol):
    """A searchable/lookupable external metadata source."""

    async def search(
        self,
        query: MetadataQuery | Mapping[str, Any],
    ) -> Sequence[ProviderObservation]:
        """Find normalized observations for a free-form query."""

    async def lookup(self, identifier: str) -> ProviderObservation:
        """Fetch one normalized observation by provider identifier."""


__all__ = [
    "AcoustIdCandidate",
    "AcoustIdLookupPayload",
    "AudioFingerprintPayload",
    "CoverCandidate",
    "CoverCandidatesPayload",
    "FingerprintTool",
    "LyricsCandidate",
    "LyricsCandidatesPayload",
    "MetadataProvider",
    "MetadataQuery",
    "MusicBrainzArtist",
    "MusicBrainzArtistResult",
    "MusicBrainzQuery",
    "MusicBrainzRecording",
    "MusicBrainzRelationship",
    "MusicBrainzRelease",
    "MusicBrainzReleaseRef",
    "MusicBrainzSearchPayload",
    "ProviderObservation",
    "ttl_expiry",
]
