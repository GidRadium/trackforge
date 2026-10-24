"""Read-only views for review, decisions, and library results."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from trackforge.application.dto.decisions import (
    AlternativeView,
    ExplanationView,
    ResolvedMetadata,
)
from trackforge.application.dto.feedback import CorrectionView, LockView, ReviewSessionView
from trackforge.domain.errors import DomainValidationError
from trackforge.domain.model._validation import (
    FieldValue,
    coerce_enum,
    ensure_datetime,
    optional_text,
)
from trackforge.domain.model.decisions import DecidedBy, DecisionMode
from trackforge.domain.model.ids import DecisionRecordId, ItemId
from trackforge.domain.model.item import ItemStatus
from trackforge.domain.model.outputs import LibraryEntry, LibraryStatus, TargetFormat
from trackforge.domain.policies.normalization import FIELD_ALIASES


@dataclass(frozen=True, slots=True)
class DecisionView:
    """One resolved field and its evidence summary."""

    field: str
    value: FieldValue
    confidence: float = 0.0
    rationale: str = ""
    mode: DecisionMode = DecisionMode.POLICY
    decided_by: DecidedBy = DecidedBy.POLICY
    locked: bool = False
    alternatives: tuple[AlternativeView, ...] = ()

    def __post_init__(self) -> None:
        field_name = optional_text(self.field, "field")
        if field_name is None:
            raise DomainValidationError("field must not be empty")
        object.__setattr__(self, "field", field_name)
        object.__setattr__(self, "mode", coerce_enum(self.mode, DecisionMode, "mode"))
        object.__setattr__(
            self,
            "decided_by",
            coerce_enum(self.decided_by, DecidedBy, "decided_by"),
        )
        object.__setattr__(self, "alternatives", tuple(self.alternatives))

    @property
    def current(self) -> FieldValue:
        return self.value

    @property
    def chosen(self) -> FieldValue:
        return self.value


@dataclass(frozen=True, slots=True)
class ResolveView:
    """Resolved metadata plus per-field decision views.

    Iterating this object yields :class:`DecisionView` instances, so callers
    can treat it either as the legacy ``ResolvedMetadata`` result or as the
    documented ``DecisionView[]``.
    """

    item_id: ItemId
    decision_record_id: DecisionRecordId
    decisions: tuple[DecisionView, ...] = ()
    fields: dict[str, FieldValue] = field(default_factory=dict)
    unknowns: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "decisions", tuple(self.decisions))
        object.__setattr__(self, "fields", dict(self.fields))
        object.__setattr__(self, "unknowns", tuple(self.unknowns))
        object.__setattr__(self, "warnings", tuple(self.warnings))

    def __iter__(self):  # type: ignore[no-untyped-def]
        return iter(self.decisions)

    def __getitem__(self, index: int) -> DecisionView:
        return self.decisions[index]

    def __len__(self) -> int:
        return len(self.decisions)

    @classmethod
    def from_resolved(cls, resolved: ResolvedMetadata) -> ResolveView:
        return cls(
            item_id=resolved.item_id,
            decision_record_id=resolved.decision_record_id,
            fields=dict(resolved.fields),
            unknowns=resolved.unknowns,
            warnings=resolved.warnings,
        )


@dataclass(frozen=True, slots=True)
class ReviewFieldView:
    """One row of the human review table."""

    field: str
    current: FieldValue
    alternatives: tuple[AlternativeView, ...] = ()
    confidence: float = 0.0
    rationale: str = ""
    mode: DecisionMode = DecisionMode.POLICY
    decided_by: DecidedBy = DecidedBy.POLICY
    locked: bool = False

    def __post_init__(self) -> None:
        field_name = optional_text(self.field, "field")
        if field_name is None:
            raise DomainValidationError("field must not be empty")
        object.__setattr__(self, "field", field_name)
        object.__setattr__(self, "alternatives", tuple(self.alternatives))
        object.__setattr__(self, "mode", coerce_enum(self.mode, DecisionMode, "mode"))
        object.__setattr__(
            self,
            "decided_by",
            coerce_enum(self.decided_by, DecidedBy, "decided_by"),
        )

    @property
    def value(self) -> FieldValue:
        return self.current


@dataclass(frozen=True, slots=True)
class ReviewView:
    """Current decisions, locks, and optional explanation for one item."""

    item_id: ItemId
    status: ItemStatus
    fields: tuple[ReviewFieldView, ...] = ()
    locked_fields: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    decision_record_id: DecisionRecordId | None = None
    explanation: ExplanationView | None = None
    action: str = "show"
    correction: CorrectionView | None = None
    lock: LockView | None = None
    session: ReviewSessionView | None = None
    actions: tuple[str, ...] = ()
    requires_render: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "status",
            coerce_enum(self.status, ItemStatus, "status"),
        )
        object.__setattr__(self, "fields", tuple(self.fields))
        object.__setattr__(self, "locked_fields", tuple(self.locked_fields))
        object.__setattr__(self, "warnings", tuple(self.warnings))
        object.__setattr__(self, "actions", tuple(self.actions))

    def field(self, name: str) -> ReviewFieldView | None:
        normalized = FIELD_ALIASES.get(name.strip().lower(), name.strip().lower())
        for field_view in self.fields:
            if field_view.field in {name, normalized}:
                return field_view
        return None

    @property
    def decisions(self) -> tuple[ReviewFieldView, ...]:
        return self.fields

    @property
    def review_session_id(self) -> str | None:
        return None if self.session is None else str(self.session.id)


# The contract in Plan 15 calls the result object ``ReviewResult``.  Keep the
# historical ``ReviewView`` name for API compatibility.
ReviewResult = ReviewView


@dataclass(frozen=True, slots=True)
class LibraryEntryView:
    """Portable view of one published library entry."""

    id: str
    item_id: ItemId
    bundle_id: str
    path: str
    status: LibraryStatus
    published_at: datetime
    target_format: TargetFormat | None = None
    sidecar_path: str | None = None
    lrc_path: str | None = None

    def __post_init__(self) -> None:
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

    @classmethod
    def from_entry(
        cls,
        entry: LibraryEntry,
        *,
        sidecar_path: str | None = None,
        lrc_path: str | None = None,
    ) -> LibraryEntryView:
        return cls(
            id=str(entry.id),
            item_id=entry.item_id,
            bundle_id=str(entry.bundle_id),
            path=entry.path,
            status=entry.status,
            published_at=entry.published_at,
            sidecar_path=sidecar_path,
            lrc_path=lrc_path,
        )


@dataclass(frozen=True, slots=True)
class ItemView:
    """Stable item summary for API callers and the status presenter."""

    item_id: ItemId
    status: ItemStatus
    media_id: str | None = None
    content_hash: str | None = None
    output_path: str | None = None
    title: str | None = None
    artist: str | None = None
    warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "status",
            coerce_enum(self.status, ItemStatus, "status"),
        )
        object.__setattr__(self, "warnings", tuple(self.warnings))


__all__ = [
    "AlternativeView",
    "DecisionView",
    "ExplanationView",
    "ItemView",
    "LibraryEntryView",
    "ResolveView",
    "ReviewFieldView",
    "ReviewResult",
    "ReviewView",
]
