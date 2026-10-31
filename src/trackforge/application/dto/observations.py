"""DTOs for local acquisition and intrinsic observations.

Observations are immutable, JSON-only artifacts.  The pydantic models in this
module are the on-disk schemas for ``audio.probe.v1``, ``initial.tags.v1`` and
``path.structure.v1``; the frozen dataclasses are read-only views returned to
callers.  Nothing here contains audio bytes or embedded artwork payloads.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from pydantic import BaseModel, ConfigDict, Field, field_validator

from trackforge.application.dto.progress import HandlerReport
from trackforge.application.ports.storage import is_sha256_hex
from trackforge.domain.model.ids import ItemId, MediaId
from trackforge.domain.model.item import ItemStatus


class MediaInput(BaseModel):
    """The pseudo-artifact that carries a media reference into the pipeline.

    Media bytes stay in CAS.  The payload is a small canonical JSON descriptor
    whose hash is used as the observer cache input.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    media_id: str = Field(min_length=1)
    content_hash: str
    size_bytes: int = Field(ge=0)
    cas_path: str = Field(min_length=1)
    container: str = Field(min_length=1)

    @field_validator("content_hash")
    @classmethod
    def _validate_content_hash(cls, value: object) -> str:
        if not is_sha256_hex(value):
            raise ValueError("content_hash must be a lowercase SHA-256 hex digest")
        return str(value)


class ProbeStream(BaseModel):
    """One normalized ffprobe stream."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    index: int | None = Field(default=None, ge=0)
    codec_type: str | None = None
    codec_name: str | None = None
    sample_rate: int | None = Field(default=None, ge=0)
    channels: int | None = Field(default=None, ge=0)
    bit_rate: int | None = Field(default=None, ge=0)
    duration_seconds: float | None = Field(default=None, ge=0.0)
    width: int | None = Field(default=None, ge=0)
    height: int | None = Field(default=None, ge=0)
    channel_layout: str | None = None


class ProbeChapter(BaseModel):
    """One chapter reported by ffprobe."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: int | None = Field(default=None, ge=0)
    start_seconds: float | None = Field(default=None, ge=0.0)
    end_seconds: float | None = Field(default=None, ge=0.0)
    title: str | None = None


class ProbePicture(BaseModel):
    """Metadata for an attached picture stream, never the picture bytes."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    index: int | None = Field(default=None, ge=0)
    codec_name: str | None = None
    mime_type: str | None = None
    width: int | None = Field(default=None, ge=0)
    height: int | None = Field(default=None, ge=0)
    size_bytes: int | None = Field(default=None, ge=0)
    description: str | None = None


class AudioProbePayload(BaseModel):
    """Schema for the ``audio.probe.v1`` payload."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    container: str = Field(min_length=1)
    format_name: str = Field(min_length=1)
    format_long_name: str | None = None
    duration_seconds: float | None = Field(default=None, ge=0.0)
    size_bytes: int | None = Field(default=None, ge=0)
    bit_rate: int | None = Field(default=None, ge=0)
    streams: list[ProbeStream] = Field(default_factory=list)
    chapters: list[ProbeChapter] = Field(default_factory=list)
    attached_pictures: list[ProbePicture] = Field(default_factory=list)

    def first_audio_stream(self) -> ProbeStream | None:
        """Return the first audio stream, if the container has one."""

        return next((stream for stream in self.streams if stream.codec_type == "audio"), None)


class ArtworkInfo(BaseModel):
    """Embedded artwork metadata; the image bytes remain in the source file."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    mime_type: str | None = None
    description: str | None = None
    picture_type: str | None = None
    width: int | None = Field(default=None, ge=0)
    height: int | None = Field(default=None, ge=0)
    size_bytes: int | None = Field(default=None, ge=0)


class NormalizedTags(BaseModel):
    """Portable, weakly-trusted view of the tags already in the file."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    title: str | None = None
    artist: list[str] = Field(default_factory=list)
    album: str | None = None
    albumartist: list[str] = Field(default_factory=list)
    date: str | None = None
    genre: list[str] = Field(default_factory=list)
    bpm: float | None = Field(default=None, gt=0.0)
    key: str | None = None
    track: int | None = Field(default=None, ge=0)
    track_total: int | None = Field(default=None, ge=0)
    disc: int | None = Field(default=None, ge=0)
    disc_total: int | None = Field(default=None, ge=0)
    comment: str | None = None
    lyrics: str | None = None
    mbids: dict[str, list[str]] = Field(default_factory=dict)
    artwork: list[ArtworkInfo] = Field(default_factory=list)


class RawTagInfo(BaseModel):
    """File-format information from mutagen, not raw tag values."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    format: str = Field(min_length=1)
    mime_types: list[str] = Field(default_factory=list)
    tag_keys: list[str] = Field(default_factory=list)


class TagReaderInfo(BaseModel):
    """The reader identity recorded in every initial-tags artifact."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1)
    version: str = Field(min_length=1)


class InitialTagsPayload(BaseModel):
    """Schema for the ``initial.tags.v1`` payload."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tags: NormalizedTags = Field(default_factory=NormalizedTags)
    raw: RawTagInfo
    reader: TagReaderInfo


class PathStructurePayload(BaseModel):
    """Schema for the ``path.structure.v1`` payload."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str = Field(min_length=1)
    parent_names: list[str] = Field(default_factory=list)
    stem: str
    suffix: str


class SourceThumbnail(BaseModel):
    """One thumbnail advertised by the source snapshot."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    url: str = Field(min_length=1)
    width: int | None = Field(default=None, ge=0)
    height: int | None = Field(default=None, ge=0)


class SourceChapter(BaseModel):
    """One chapter in a normalized source metadata payload."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    start_seconds: float | None = Field(default=None, ge=0.0)
    end_seconds: float | None = Field(default=None, ge=0.0)
    title: str | None = None


class SourceMetadataPayload(BaseModel):
    """Schema for the ``source.metadata.v1`` normalized payload.

    The raw extractor document remains the ``source.snapshot.v1`` artifact;
    this payload is the small, stable subset that interpreters consume.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    external_id: str = Field(min_length=1)
    extractor: str | None = None
    title: str = Field(min_length=1)
    uploader: str | None = None
    artist: str | None = None
    album: str | None = None
    track: str | None = None
    release_year: int | None = Field(default=None, ge=0)
    language: str | None = None
    description: str | None = None
    upload_date: str | None = None
    tags: list[str] = Field(default_factory=list)
    duration: float | None = Field(default=None, ge=0.0)
    duration_seconds: float | None = Field(default=None, ge=0.0)
    thumbnails: list[SourceThumbnail] = Field(default_factory=list)
    chapters: list[SourceChapter] = Field(default_factory=list)
    webpage_url: str | None = None


class SourceIdentityPayload(BaseModel):
    """Schema for the ``source.identity.v1`` LLM/heuristic extraction."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    title: str | None = None
    artist: str | None = None
    originalartist: str | None = None
    qualifier: str | None = None
    source_title: str | None = None
    uploader: str | None = None
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    method: str = Field(default="heuristic", min_length=1)
    warnings: list[str] = Field(default_factory=list)


@dataclass(frozen=True, slots=True)
class ObservationWarning:
    """A user-visible warning attached to an observation run."""

    code: str
    message: str
    user_action: str | None = None


@dataclass(frozen=True, slots=True)
class ArtifactSummary:
    """An observation artifact produced during one observe call."""

    type: str
    artifact_id: str
    cache_key: str
    payload_hash: str
    cached: bool = False


@dataclass(frozen=True, slots=True)
class ObservationStepView:
    """Outcome of one capability step in an observe run."""

    node_id: str
    capability_id: str
    status: str
    attempts: int = 0
    warning_codes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class MediaSummary:
    """Measured media properties after observation."""

    media_id: MediaId
    content_hash: str
    container: str
    codec: str | None = None
    bitrate: int | None = None
    sample_rate: int | None = None
    channels: int | None = None
    duration_seconds: float | None = None
    probe_artifact_id: str | None = None


@dataclass(frozen=True, slots=True)
class ObservationView:
    """Read-only result of :class:`~trackforge.application.use_cases.observe_item.ObserveItem`."""

    item_id: ItemId
    status: ItemStatus
    run_id: str
    media: MediaSummary
    artifacts: tuple[ArtifactSummary, ...] = ()
    steps: tuple[ObservationStepView, ...] = ()
    warnings: tuple[ObservationWarning, ...] = field(default_factory=tuple)
    step_reports: tuple[HandlerReport, ...] = ()

    @property
    def cache_hit(self) -> bool:
        """Return whether every step in this run was served from cache."""

        return bool(self.steps) and all(step.status == "cached" for step in self.steps)

    def artifact(self, artifact_type: str) -> ArtifactSummary | None:
        """Return one artifact summary by its type string."""

        return next(
            (artifact for artifact in self.artifacts if artifact.type == artifact_type),
            None,
        )


__all__ = [
    "ArtifactSummary",
    "ArtworkInfo",
    "AudioProbePayload",
    "InitialTagsPayload",
    "MediaInput",
    "MediaSummary",
    "NormalizedTags",
    "ObservationStepView",
    "ObservationView",
    "ObservationWarning",
    "PathStructurePayload",
    "ProbeChapter",
    "ProbePicture",
    "ProbeStream",
    "RawTagInfo",
    "SourceChapter",
    "SourceIdentityPayload",
    "SourceMetadataPayload",
    "SourceThumbnail",
    "TagReaderInfo",
]
