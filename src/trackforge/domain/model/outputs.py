"""Resolved metadata and render/publish output records."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from trackforge.domain.errors import DomainValidationError
from trackforge.domain.model._validation import (
    FieldValue,
    coerce_enum,
    coerce_path,
    ensure_datetime,
    ensure_sha256,
    ensure_str_tuple,
    json_mapping,
    require_id,
    require_text,
    utc_now,
    validate_field_value,
)
from trackforge.domain.model.ids import (
    BundleId,
    DecisionRecordId,
    ItemId,
    LibraryEntryId,
    RenderId,
)


@dataclass(frozen=True, slots=True)
class ResolvedMetadata:
    """Canonical fields produced by resolution, ready for rendering."""

    item_id: ItemId
    decision_record_id: DecisionRecordId
    fields: dict[str, FieldValue] = field(default_factory=dict)
    unknowns: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "item_id", require_id(self.item_id, "item_id"))
        object.__setattr__(
            self,
            "decision_record_id",
            require_id(self.decision_record_id, "decision_record_id"),
        )
        object.__setattr__(self, "fields", _metadata_fields(self.fields))
        object.__setattr__(self, "unknowns", ensure_str_tuple(self.unknowns, "unknowns"))
        object.__setattr__(self, "warnings", ensure_str_tuple(self.warnings, "warnings"))


def _metadata_fields(value: object) -> dict[str, FieldValue]:
    if not isinstance(value, Mapping):
        raise DomainValidationError(
            "fields must be a mapping",
            context={"actual_type": type(value).__name__},
        )
    result: dict[str, FieldValue] = {}
    for raw_key, raw_value in value.items():
        key = require_text(raw_key, "fields key")
        result[key] = validate_field_value(raw_value, f"fields.{key}")
    return result


class TargetFormat(StrEnum):
    MP3 = "mp3"
    M4A = "m4a"
    FLAC = "flac"
    OGG = "ogg"
    OPUS = "opus"


RenderFormat = TargetFormat
OutputFormat = TargetFormat


class RenderStatus(StrEnum):
    PLANNED = "planned"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class Render:
    """A render plan/operation; actual files live in an output bundle."""

    id: RenderId
    item_id: ItemId
    decision_record_id: DecisionRecordId
    target_format: TargetFormat
    naming_template: str
    tag_mapping_version: str
    options: dict[str, Any] = field(default_factory=dict)
    status: RenderStatus = RenderStatus.PLANNED
    created_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", require_id(self.id, "id"))
        object.__setattr__(self, "item_id", require_id(self.item_id, "item_id"))
        object.__setattr__(
            self,
            "decision_record_id",
            require_id(self.decision_record_id, "decision_record_id"),
        )
        object.__setattr__(
            self,
            "target_format",
            coerce_enum(self.target_format, TargetFormat, "target_format"),
        )
        object.__setattr__(
            self,
            "naming_template",
            require_text(self.naming_template, "naming_template"),
        )
        object.__setattr__(
            self,
            "tag_mapping_version",
            require_text(self.tag_mapping_version, "tag_mapping_version"),
        )
        object.__setattr__(self, "options", json_mapping(self.options, "options"))
        object.__setattr__(
            self,
            "status",
            coerce_enum(self.status, RenderStatus, "status"),
        )
        object.__setattr__(
            self,
            "created_at",
            ensure_datetime(self.created_at, "created_at"),
        )


@dataclass(frozen=True, slots=True)
class OutputBundle:
    """A concrete set of rendered files and their bundle hash."""

    id: BundleId
    render_id: RenderId
    files: tuple[str, ...]
    bundle_hash: str
    created_at: datetime = field(default_factory=utc_now)
    item_id: ItemId | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", require_id(self.id, "id"))
        object.__setattr__(self, "render_id", require_id(self.render_id, "render_id"))
        object.__setattr__(
            self,
            "files",
            ensure_str_tuple(self.files, "files", allow_empty=False),
        )
        object.__setattr__(
            self,
            "bundle_hash",
            ensure_sha256(self.bundle_hash, "bundle_hash"),
        )
        object.__setattr__(
            self,
            "created_at",
            ensure_datetime(self.created_at, "created_at"),
        )
        if self.item_id is not None:
            object.__setattr__(self, "item_id", require_id(self.item_id, "item_id"))


class LibraryStatus(StrEnum):
    PUBLISHED = "published"
    MISSING = "missing"
    MODIFIED = "modified"
    DUPLICATE = "duplicate"


@dataclass(frozen=True, slots=True)
class LibraryEntry:
    """A published bundle tracked in the managed library."""

    id: LibraryEntryId
    item_id: ItemId
    bundle_id: BundleId
    path: str
    status: LibraryStatus
    published_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", require_id(self.id, "id"))
        object.__setattr__(self, "item_id", require_id(self.item_id, "item_id"))
        object.__setattr__(self, "bundle_id", require_id(self.bundle_id, "bundle_id"))
        object.__setattr__(self, "path", coerce_path(self.path, "path"))
        object.__setattr__(
            self,
            "status",
            coerce_enum(self.status, LibraryStatus, "status"),
        )
        object.__setattr__(
            self,
            "published_at",
            ensure_datetime(self.published_at, "published_at"),
        )


__all__ = [
    "LibraryEntry",
    "LibraryStatus",
    "OutputBundle",
    "OutputFormat",
    "Render",
    "RenderFormat",
    "RenderStatus",
    "ResolvedMetadata",
    "TargetFormat",
]
