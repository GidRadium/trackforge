"""Observations, claims, provenance, and calibrated confidence."""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field as dc_field
from datetime import datetime
from enum import StrEnum
from typing import Any

from trackforge.domain.errors import DomainValidationError
from trackforge.domain.model._validation import (
    FieldValue,
    coerce_enum,
    ensure_datetime,
    ensure_id_tuple,
    ensure_int,
    ensure_sha256,
    ensure_str_tuple,
    json_mapping,
    optional_id,
    optional_text,
    require_id,
    require_text,
    utc_now,
    validate_confidence,
    validate_field_value,
)
from trackforge.domain.model.capabilities import Cost
from trackforge.domain.model.ids import ClaimId, ItemId, ObservationId

_UNSET: Any = object()


class Confidence(float):
    """Calibrated confidence in the inclusive ``[0, 1]`` range."""

    def __new__(cls, value: float | int = 0.0) -> Confidence:
        validated = validate_confidence(value)
        return super().__new__(cls, validated)


EvidenceRef = str


class Subject(StrEnum):
    """The entity a claim is about; claims from different subjects never merge."""

    ITEM = "item"
    MEDIA = "media"
    RECORDING = "recording"
    RELEASE = "release"
    RELEASE_GROUP = "release_group"
    WORK = "work"
    ARTIST = "artist"
    DERIVATIVE = "derivative"
    COLLECTION = "collection"


ClaimSubject = Subject


class ClaimStatus(StrEnum):
    PROPOSED = "proposed"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"


@dataclass(frozen=True, slots=True)
class Provenance:
    """Where a claim came from and how it was produced."""

    source: str
    method: str
    observed_at: datetime
    cost: Cost | None = None
    evidence: tuple[EvidenceRef, ...] = ()
    evidence_reason: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "source", require_text(self.source, "source"))
        object.__setattr__(self, "method", require_text(self.method, "method"))
        object.__setattr__(
            self,
            "observed_at",
            ensure_datetime(self.observed_at, "observed_at"),
        )
        if self.cost is not None and not isinstance(self.cost, Cost):
            raise DomainValidationError("cost must be a Cost")
        object.__setattr__(self, "evidence", ensure_str_tuple(self.evidence, "evidence"))
        object.__setattr__(
            self,
            "evidence_reason",
            optional_text(self.evidence_reason, "evidence_reason"),
        )


@dataclass(frozen=True, slots=True)
class Claim:
    """An append-only assertion about one field of one subject."""

    id: ClaimId
    item_id: ItemId
    subject: Subject
    subject_id: str | None = None
    field: str = ""
    value: FieldValue = None
    role: str | None = None
    order: int | None = None
    locale: str | None = None
    source: str = ""
    method: str = ""
    confidence: Confidence = _UNSET
    evidence: tuple[EvidenceRef, ...] = ()
    derived_from: tuple[ClaimId, ...] = ()
    status: ClaimStatus = ClaimStatus.PROPOSED
    notes: str | None = None
    created_at: datetime = dc_field(default_factory=utc_now)
    evidence_reason: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", require_id(self.id, "id"))
        object.__setattr__(self, "item_id", require_id(self.item_id, "item_id"))
        object.__setattr__(self, "subject", coerce_enum(self.subject, Subject, "subject"))
        object.__setattr__(self, "field", require_text(self.field, "field"))
        object.__setattr__(self, "value", validate_field_value(self.value, "value"))
        object.__setattr__(self, "source", require_text(self.source, "source"))
        object.__setattr__(self, "method", require_text(self.method, "method"))
        if self.confidence is _UNSET:
            raise DomainValidationError(
                "claim confidence is required",
                context={"claim_id": self.id, "field": self.field},
            )
        object.__setattr__(
            self,
            "confidence",
            Confidence(self.confidence),
        )
        object.__setattr__(self, "subject_id", optional_id(self.subject_id, "subject_id"))
        object.__setattr__(self, "role", optional_text(self.role, "role"))
        if self.order is not None:
            object.__setattr__(
                self,
                "order",
                ensure_int(self.order, "order", minimum=0),
            )
        object.__setattr__(self, "locale", optional_text(self.locale, "locale"))
        evidence = ensure_str_tuple(self.evidence, "evidence")
        reason = optional_text(self.evidence_reason, "evidence_reason")
        notes = optional_text(self.notes, "notes")
        if not evidence and reason is None and notes is None:
            raise DomainValidationError(
                "claim evidence is required unless evidence_reason or notes "
                "explains why the source snapshot is the evidence",
                context={"claim_id": self.id, "field": self.field},
            )
        object.__setattr__(self, "evidence", evidence)
        object.__setattr__(self, "evidence_reason", reason)
        object.__setattr__(self, "notes", notes)
        derived_from = ensure_id_tuple(self.derived_from, "derived_from")
        if self.id in derived_from:
            raise DomainValidationError(
                "a claim cannot be derived from itself",
                context={"claim_id": self.id},
            )
        object.__setattr__(self, "derived_from", derived_from)
        object.__setattr__(
            self,
            "status",
            coerce_enum(self.status, ClaimStatus, "status"),
        )
        object.__setattr__(
            self,
            "created_at",
            ensure_datetime(self.created_at, "created_at"),
        )

    @property
    def is_synthesized(self) -> bool:
        return bool(self.derived_from)

    @property
    def is_unknown(self) -> bool:
        return self.value is None


@dataclass(frozen=True, slots=True)
class Observation:
    """An immutable raw measurement or source response."""

    id: ObservationId
    item_id: ItemId
    type: str
    schema_version: int
    producer: str
    inputs: tuple[str, ...] = ()
    params: dict[str, object] = dc_field(default_factory=dict)
    payload: dict[str, object] = dc_field(default_factory=dict)
    warnings: tuple[str, ...] = ()
    created_at: datetime = dc_field(default_factory=utc_now)
    content_hash: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", require_id(self.id, "id"))
        object.__setattr__(self, "item_id", require_id(self.item_id, "item_id"))
        object.__setattr__(self, "type", require_text(self.type, "type"))
        object.__setattr__(
            self,
            "schema_version",
            ensure_int(self.schema_version, "schema_version", minimum=1),
        )
        object.__setattr__(self, "producer", require_text(self.producer, "producer"))
        object.__setattr__(self, "inputs", ensure_str_tuple(self.inputs, "inputs"))
        object.__setattr__(self, "params", json_mapping(self.params, "params"))
        object.__setattr__(self, "payload", json_mapping(self.payload, "payload"))
        object.__setattr__(self, "warnings", ensure_str_tuple(self.warnings, "warnings"))
        object.__setattr__(
            self,
            "created_at",
            ensure_datetime(self.created_at, "created_at"),
        )
        if self.content_hash is not None:
            object.__setattr__(
                self,
                "content_hash",
                ensure_sha256(self.content_hash, "content_hash"),
            )

    @property
    def artifact_type(self) -> str:
        return f"{self.type}.v{self.schema_version}"


__all__ = [
    "Claim",
    "ClaimStatus",
    "ClaimSubject",
    "Confidence",
    "EvidenceRef",
    "Observation",
    "Provenance",
    "Subject",
]
