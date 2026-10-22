"""Immutable media objects, file instances, and acquisition intakes."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from trackforge.domain.errors import DomainValidationError
from trackforge.domain.model._validation import (
    coerce_enum,
    coerce_path,
    ensure_datetime,
    ensure_float,
    ensure_int,
    ensure_sha256,
    optional_id,
    optional_text,
    require_id,
    require_text,
    utc_now,
)
from trackforge.domain.model.ids import (
    ArtifactId,
    FileInstanceId,
    IntakeId,
    ItemId,
    MediaId,
)
from trackforge.domain.model.source import (
    Candidate,
    CollectionRef,
    LocalPath,
    SourceRef,
    TextQuery,
    Url,
)


class MediaContainer(StrEnum):
    MP3 = "mp3"
    M4A = "m4a"
    M4B = "m4b"
    MP4 = "mp4"
    FLAC = "flac"
    OGG = "ogg"
    OGA = "oga"
    OPUS = "opus"
    WAV = "wav"
    AAC = "aac"
    ALAC = "alac"
    WEBM = "webm"
    MKV = "mkv"
    MKA = "mka"
    AIFF = "aiff"
    WV = "wv"


@dataclass(frozen=True, slots=True)
class MediaObject:
    """Immutable content-addressed audio bytes and their measured properties."""

    id: MediaId
    content_hash: str
    size_bytes: int
    cas_path: str
    container: MediaContainer
    codec: str | None = None
    bitrate: int | None = None
    sample_rate: int | None = None
    channels: int | None = None
    duration_seconds: float | None = None
    probe_artifact_id: ArtifactId | None = None
    created_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", require_id(self.id, "id"))
        object.__setattr__(
            self,
            "content_hash",
            ensure_sha256(self.content_hash, "content_hash"),
        )
        object.__setattr__(
            self,
            "size_bytes",
            ensure_int(self.size_bytes, "size_bytes", minimum=0),
        )
        object.__setattr__(self, "cas_path", coerce_path(self.cas_path, "cas_path"))
        object.__setattr__(
            self,
            "container",
            coerce_enum(self.container, MediaContainer, "container"),
        )
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
        object.__setattr__(
            self,
            "probe_artifact_id",
            optional_id(self.probe_artifact_id, "probe_artifact_id"),
        )
        object.__setattr__(
            self,
            "created_at",
            ensure_datetime(self.created_at, "created_at"),
        )


class FileRole(StrEnum):
    SOURCE = "source"
    CAS = "cas"
    RENDER = "render"
    LIBRARY = "library"
    DUPLICATE = "duplicate"


Mtime = datetime | float


@dataclass(frozen=True, slots=True)
class FileInstance:
    """A concrete appearance of media bytes on disk."""

    id: FileInstanceId
    media_id: MediaId
    path: str
    role: FileRole
    size_bytes: int
    mtime: Mtime
    intake_id: IntakeId | None = None
    created_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", require_id(self.id, "id"))
        object.__setattr__(self, "media_id", require_id(self.media_id, "media_id"))
        object.__setattr__(self, "path", coerce_path(self.path, "path"))
        object.__setattr__(self, "role", coerce_enum(self.role, FileRole, "role"))
        object.__setattr__(
            self,
            "size_bytes",
            ensure_int(self.size_bytes, "size_bytes", minimum=0),
        )
        if isinstance(self.mtime, datetime):
            pass
        elif isinstance(self.mtime, bool) or not isinstance(self.mtime, (int, float)):
            raise DomainValidationError(
                "mtime must be a datetime or POSIX timestamp",
                context={"actual_type": type(self.mtime).__name__},
            )
        else:
            object.__setattr__(
                self,
                "mtime",
                ensure_float(self.mtime, "mtime", minimum=0.0),
            )
        object.__setattr__(
            self,
            "intake_id",
            optional_id(self.intake_id, "intake_id"),
        )
        object.__setattr__(
            self,
            "created_at",
            ensure_datetime(self.created_at, "created_at"),
        )


class IntakeStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class Intake:
    """One acquisition fact for an item."""

    id: IntakeId
    item_id: ItemId
    source_ref: SourceRef
    candidate: Candidate
    source_snapshot_artifact_id: ArtifactId | None = None
    downloader: str = ""
    downloader_version: str = ""
    media_id: MediaId | None = None
    status: IntakeStatus = IntakeStatus.SUCCEEDED
    created_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", require_id(self.id, "id"))
        object.__setattr__(self, "item_id", require_id(self.item_id, "item_id"))
        if not isinstance(self.source_ref, (LocalPath, Url, TextQuery, CollectionRef)):
            raise DomainValidationError("source_ref must be a SourceRef")
        if not isinstance(self.candidate, Candidate):
            raise DomainValidationError("candidate must be a Candidate")
        object.__setattr__(
            self,
            "downloader",
            require_text(self.downloader, "downloader"),
        )
        object.__setattr__(
            self,
            "downloader_version",
            require_text(self.downloader_version, "downloader_version"),
        )
        object.__setattr__(
            self,
            "source_snapshot_artifact_id",
            optional_id(self.source_snapshot_artifact_id, "source_snapshot_artifact_id"),
        )
        object.__setattr__(self, "media_id", optional_id(self.media_id, "media_id"))
        object.__setattr__(
            self,
            "status",
            coerce_enum(self.status, IntakeStatus, "status"),
        )
        object.__setattr__(
            self,
            "created_at",
            ensure_datetime(self.created_at, "created_at"),
        )


__all__ = [
    "FileInstance",
    "FileRole",
    "Intake",
    "IntakeStatus",
    "MediaContainer",
    "MediaObject",
    "Mtime",
]
