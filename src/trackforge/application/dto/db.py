"""DTOs for catalog rows that are not (yet) domain entities.

Items, media objects, intakes, claims, decisions, renders, bundles, and
library entries already have domain types.  Runs, steps, artifact metadata,
locks, and feedback statistics are persistence-bound concepts in Plan 04, so
they are represented by small immutable DTOs at the application boundary.

Timestamps are timezone-aware UTC :class:`datetime` objects in Python and
fixed-width ISO 8601 strings with a ``Z`` suffix in SQLite.
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field as dc_field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from trackforge.application.dto.artifacts import ArtifactInput, ArtifactManifest, ArtifactProducer
from trackforge.application.services.canonical_json import canonical_json
from trackforge.domain.errors import DomainValidationError
from trackforge.domain.model._validation import (
    FieldValue,
    coerce_enum,
    ensure_datetime,
    ensure_int,
    json_mapping,
    optional_id,
    require_id,
    require_text,
    utc_now,
    validate_field_value,
)
from trackforge.domain.model.claims import ClaimStatus
from trackforge.domain.model.ids import ClaimId, ItemId, RunId, StepId


class RunStatus(StrEnum):
    """Lifecycle of one pipeline run."""

    PLANNED = "planned"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class StepStatus(StrEnum):
    """Lifecycle of one capability step."""

    PENDING = "pending"
    READY = "ready"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"
    CACHED = "cached"


class RunItemStatus(StrEnum):
    """Lifecycle of an item inside a run."""

    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"


def _json_object(value: object, field_name: str) -> dict[str, Any] | None:
    if value is None:
        return None
    return dict(json_mapping(value, field_name))


def _json_warnings(value: object, field_name: str) -> tuple[dict[str, Any] | str, ...]:
    if isinstance(value, (str, bytes, bytearray)):
        raise DomainValidationError(
            f"{field_name} must be a sequence of warning objects or strings",
            context={"field": field_name},
        )
    try:
        items: tuple[object, ...] = tuple(value)  # type: ignore[arg-type]
    except TypeError:
        raise DomainValidationError(
            f"{field_name} must be a sequence of warning objects or strings",
            context={"field": field_name},
        ) from None
    result: list[dict[str, Any] | str] = []
    for index, item in enumerate(items):
        if isinstance(item, str):
            result.append(item)
        else:
            result.append(dict(json_mapping(item, f"{field_name}[{index}]")))
    return tuple(result)


def _json_objects(value: object, field_name: str) -> tuple[dict[str, Any], ...]:
    if isinstance(value, (str, bytes, bytearray)):
        raise DomainValidationError(
            f"{field_name} must be a sequence of JSON objects",
            context={"field": field_name},
        )
    try:
        items: tuple[object, ...] = tuple(value)  # type: ignore[arg-type]
    except TypeError:
        raise DomainValidationError(
            f"{field_name} must be a sequence of JSON objects",
            context={"field": field_name},
        ) from None
    result: list[dict[str, Any]] = []
    for index, item in enumerate(items):
        result.append(dict(json_mapping(item, f"{field_name}[{index}]")))
    return tuple(result)


@dataclass(frozen=True, slots=True)
class ArtifactRecord:
    """Persistable metadata for one cached artifact.

    The payload itself lives in the artifact store; this row is the catalog
    index over it.  ``to_manifest`` reconstructs the shared manifest schema,
    while ``from_manifest`` is the normal write-side adapter.
    """

    id: str
    artifact_type: str
    schema_version: int
    producer_id: str
    producer_version: str
    cache_key: str
    payload_path: str
    payload_hash: str
    inputs: tuple[dict[str, Any], ...] = ()
    params: dict[str, Any] = dc_field(default_factory=dict)
    params_hash: str = ""
    warnings: tuple[dict[str, Any], ...] = ()
    license: str | None = None
    created_at: datetime = dc_field(default_factory=utc_now)
    expires_at: datetime | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", require_id(self.id, "id"))
        object.__setattr__(self, "artifact_type", require_text(self.artifact_type, "type"))
        object.__setattr__(
            self,
            "schema_version",
            ensure_int(self.schema_version, "schema_version", minimum=1),
        )
        object.__setattr__(
            self,
            "producer_id",
            require_text(self.producer_id, "producer_id"),
        )
        object.__setattr__(
            self,
            "producer_version",
            require_text(self.producer_version, "producer_version"),
        )
        object.__setattr__(self, "cache_key", require_text(self.cache_key, "cache_key"))
        object.__setattr__(
            self,
            "payload_path",
            require_text(self.payload_path, "payload_path"),
        )
        object.__setattr__(
            self,
            "payload_hash",
            require_text(self.payload_hash, "payload_hash"),
        )
        object.__setattr__(self, "inputs", _json_objects(self.inputs, "inputs"))
        object.__setattr__(self, "params", _json_object(self.params, "params") or {})
        object.__setattr__(
            self,
            "params_hash",
            require_text(self.params_hash, "params_hash") if self.params_hash else "",
        )
        object.__setattr__(self, "warnings", _json_objects(self.warnings, "warnings"))
        if self.license is not None:
            object.__setattr__(self, "license", require_text(self.license, "license"))
        object.__setattr__(
            self,
            "created_at",
            ensure_datetime(self.created_at, "created_at"),
        )
        if self.expires_at is not None:
            object.__setattr__(
                self,
                "expires_at",
                ensure_datetime(self.expires_at, "expires_at"),
            )
        canonical_json(self.params)
        canonical_json(list(self.inputs))
        canonical_json(list(self.warnings))

    @property
    def type(self) -> str:
        """Alias matching the artifact manifest field name."""

        return self.artifact_type

    @classmethod
    def from_manifest(
        cls,
        manifest: ArtifactManifest,
        *,
        cache_key: str,
        payload_path: str,
    ) -> ArtifactRecord:
        """Build a catalog record from an artifact manifest and store ref."""

        return cls(
            id=manifest.artifact_id,
            artifact_type=manifest.type,
            schema_version=manifest.schema_version,
            producer_id=manifest.producer.id,
            producer_version=manifest.producer.version,
            cache_key=cache_key,
            payload_path=payload_path,
            payload_hash=manifest.payload_hash,
            inputs=tuple(item.model_dump(mode="json") for item in manifest.inputs),
            params=dict(manifest.params),
            params_hash=manifest.params_hash,
            warnings=tuple(dict(item) for item in manifest.warnings),
            license=manifest.license,
            created_at=_parse_manifest_time(manifest.created_at),
            expires_at=(
                _parse_manifest_time(manifest.expires_at)
                if manifest.expires_at is not None
                else None
            ),
        )

    def to_manifest(self) -> ArtifactManifest:
        """Reconstruct the shared artifact manifest schema."""

        return ArtifactManifest(
            artifact_id=self.id,
            type=self.artifact_type,
            schema_version=self.schema_version,
            producer=ArtifactProducer(id=self.producer_id, version=self.producer_version),
            inputs=[ArtifactInput(**item) for item in self.inputs],
            params=self.params,
            params_hash=self.params_hash,
            payload_hash=self.payload_hash,
            warnings=list(self.warnings),
            created_at=_format_manifest_time(self.created_at),
            expires_at=(
                _format_manifest_time(self.expires_at) if self.expires_at is not None else None
            ),
            license=self.license,
        )


def _parse_manifest_time(value: str) -> datetime:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:  # pragma: no cover - manifests are validated upstream
        raise DomainValidationError(
            "manifest timestamp must be ISO 8601",
            context={"value": value},
        ) from exc


def _format_manifest_time(value: datetime) -> str:
    normalized = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    return normalized.isoformat(timespec="microseconds").replace("+00:00", "Z")


@dataclass(frozen=True, slots=True)
class RunRecord:
    """One pipeline run row."""

    id: RunId
    profile: str
    mode: str
    status: RunStatus = RunStatus.PLANNED
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error: dict[str, Any] | None = None
    config_snapshot_hash: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", require_id(self.id, "id"))
        object.__setattr__(self, "profile", require_text(self.profile, "profile"))
        object.__setattr__(self, "mode", require_text(self.mode, "mode"))
        object.__setattr__(self, "status", coerce_enum(self.status, RunStatus, "status"))
        if self.started_at is not None:
            object.__setattr__(
                self,
                "started_at",
                ensure_datetime(self.started_at, "started_at"),
            )
        if self.finished_at is not None:
            object.__setattr__(
                self,
                "finished_at",
                ensure_datetime(self.finished_at, "finished_at"),
            )
        object.__setattr__(self, "error", _json_object(self.error, "error"))
        if self.config_snapshot_hash is not None:
            object.__setattr__(
                self,
                "config_snapshot_hash",
                require_text(self.config_snapshot_hash, "config_snapshot_hash"),
            )


@dataclass(frozen=True, slots=True)
class StepRecord:
    """One capability step row."""

    id: StepId
    run_id: RunId
    item_id: ItemId
    capability_id: str
    capability_version: str
    cache_key: str | None = None
    status: StepStatus = StepStatus.PENDING
    attempts: int = 0
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error: dict[str, Any] | None = None
    warnings: tuple[dict[str, Any] | str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", require_id(self.id, "id"))
        object.__setattr__(self, "run_id", require_id(self.run_id, "run_id"))
        object.__setattr__(self, "item_id", require_id(self.item_id, "item_id"))
        object.__setattr__(
            self,
            "capability_id",
            require_text(self.capability_id, "capability_id"),
        )
        object.__setattr__(
            self,
            "capability_version",
            require_text(self.capability_version, "capability_version"),
        )
        if self.cache_key is not None:
            object.__setattr__(self, "cache_key", require_text(self.cache_key, "cache_key"))
        object.__setattr__(self, "status", coerce_enum(self.status, StepStatus, "status"))
        object.__setattr__(
            self,
            "attempts",
            ensure_int(self.attempts, "attempts", minimum=0),
        )
        if self.started_at is not None:
            object.__setattr__(
                self,
                "started_at",
                ensure_datetime(self.started_at, "started_at"),
            )
        if self.finished_at is not None:
            object.__setattr__(
                self,
                "finished_at",
                ensure_datetime(self.finished_at, "finished_at"),
            )
        object.__setattr__(self, "error", _json_object(self.error, "error"))
        object.__setattr__(self, "warnings", _json_warnings(self.warnings, "warnings"))


@dataclass(frozen=True, slots=True)
class RunItemRecord:
    """Association between a run and one item."""

    run_id: RunId
    item_id: ItemId
    status: RunItemStatus = RunItemStatus.PENDING

    def __post_init__(self) -> None:
        object.__setattr__(self, "run_id", require_id(self.run_id, "run_id"))
        object.__setattr__(self, "item_id", require_id(self.item_id, "item_id"))
        object.__setattr__(
            self,
            "status",
            coerce_enum(self.status, RunItemStatus, "status"),
        )


@dataclass(frozen=True, slots=True)
class LockRecord:
    """A user lock for one item/field pair; ``field=''`` means item-level."""

    id: str
    item_id: ItemId
    field: str
    value: FieldValue
    source_claim_id: ClaimId | None = None
    created_at: datetime = dc_field(default_factory=utc_now)
    updated_at: datetime = dc_field(default_factory=utc_now)

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", require_id(self.id, "id"))
        object.__setattr__(self, "item_id", require_id(self.item_id, "item_id"))
        if not isinstance(self.field, str):
            raise DomainValidationError("field must be a string")
        object.__setattr__(self, "value", validate_field_value(self.value, "value"))
        object.__setattr__(
            self,
            "source_claim_id",
            optional_id(self.source_claim_id, "source_claim_id"),
        )
        object.__setattr__(
            self,
            "created_at",
            ensure_datetime(self.created_at, "created_at"),
        )
        object.__setattr__(
            self,
            "updated_at",
            ensure_datetime(self.updated_at, "updated_at"),
        )


@dataclass(frozen=True, slots=True)
class ClaimEventRecord:
    """One append-only claim status event."""

    id: str
    claim_id: ClaimId
    status: ClaimStatus
    reason: str | None = None
    created_at: datetime = dc_field(default_factory=utc_now)

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", require_id(self.id, "id"))
        object.__setattr__(self, "claim_id", require_id(self.claim_id, "claim_id"))
        object.__setattr__(
            self,
            "status",
            coerce_enum(self.status, ClaimStatus, "status"),
        )
        if self.reason is not None:
            object.__setattr__(self, "reason", require_text(self.reason, "reason"))
        object.__setattr__(
            self,
            "created_at",
            ensure_datetime(self.created_at, "created_at"),
        )


@dataclass(frozen=True, slots=True)
class FeedbackStats:
    """Aggregated feedback counts for reporting.

    ``by_source``/``by_method`` describe the evidence behind the reviewed
    decision (for example ``musicbrainz``/``musicbrainz-api/v1``).  They are
    derived from the event's context snapshot, so older events without a
    snapshot land in ``unknown`` rather than being silently dropped.
    """

    total: int
    by_kind: dict[str, int] = dc_field(default_factory=dict)
    by_field: dict[str, int] = dc_field(default_factory=dict)
    by_source: dict[str, int] = dc_field(default_factory=dict)
    by_method: dict[str, int] = dc_field(default_factory=dict)
    corrections: int = 0
    approvals: int = 0
    rejections: int = 0
    locks: int = 0
    unlocks: int = 0
    review_notes: int = 0
    unknowns: int = 0

    @property
    def correction_rate(self) -> float:
        reviewed = self.corrections + self.approvals + self.rejections
        return 0.0 if reviewed <= 0 else self.corrections / reviewed

    @property
    def review_rate(self) -> float:
        return (
            0.0
            if self.total <= 0
            else (self.corrections + self.approvals + self.rejections) / self.total
        )

    @property
    def unknown_rate(self) -> float:
        return 0.0 if self.total <= 0 else self.unknowns / self.total


__all__ = [
    "ArtifactRecord",
    "ClaimEventRecord",
    "FeedbackStats",
    "LockRecord",
    "RunItemRecord",
    "RunItemStatus",
    "RunStatus",
    "RunRecord",
    "StepRecord",
    "StepStatus",
]
