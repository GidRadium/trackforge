"""Read-only DTOs for review, feedback export, and feedback statistics.

The persisted feedback event is a domain object.  These views add the
presentation/export-friendly fields (correction and lock summaries, stable
statistics) without leaking SQLAlchemy or filesystem types into callers.
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field as dc_field
from datetime import datetime
from pathlib import Path
from typing import Any

from trackforge.domain.model._validation import (
    FieldValue,
    coerce_enum,
    ensure_datetime,
    optional_text,
    require_id,
    require_text,
    utc_now,
    validate_field_value,
)
from trackforge.domain.model.feedback import CorrectionScope, ReasonCode
from trackforge.domain.model.ids import (
    ClaimId,
    CorrectionId,
    DecisionId,
    ItemId,
    ReviewSessionId,
)


@dataclass(frozen=True, slots=True)
class CorrectionView:
    """Portable summary of one persisted correction."""

    id: CorrectionId
    item_id: ItemId
    field: str
    system_decision_id: DecisionId
    system_value: FieldValue
    user_value: FieldValue
    reason_code: ReasonCode
    scope: CorrectionScope = CorrectionScope.ITEM
    reason_text: str | None = None
    user_claim_id: ClaimId | None = None
    review_session_id: ReviewSessionId | None = None
    created_at: datetime = dc_field(default_factory=utc_now)
    applied: bool = False
    context_snapshot: dict[str, Any] = dc_field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", require_id(self.id, "id"))
        object.__setattr__(self, "item_id", require_id(self.item_id, "item_id"))
        object.__setattr__(self, "field", require_text(self.field, "field"))
        object.__setattr__(
            self,
            "system_decision_id",
            require_id(self.system_decision_id, "system_decision_id"),
        )
        object.__setattr__(
            self,
            "system_value",
            validate_field_value(self.system_value, "system_value"),
        )
        object.__setattr__(
            self,
            "user_value",
            validate_field_value(self.user_value, "user_value"),
        )
        object.__setattr__(
            self,
            "reason_code",
            coerce_enum(self.reason_code, ReasonCode, "reason_code"),
        )
        object.__setattr__(self, "scope", coerce_enum(self.scope, CorrectionScope, "scope"))
        object.__setattr__(self, "reason_text", optional_text(self.reason_text, "reason_text"))
        object.__setattr__(
            self,
            "user_claim_id",
            ClaimId(self.user_claim_id) if self.user_claim_id else None,
        )
        object.__setattr__(
            self,
            "review_session_id",
            ReviewSessionId(self.review_session_id) if self.review_session_id else None,
        )
        object.__setattr__(self, "created_at", ensure_datetime(self.created_at, "created_at"))
        object.__setattr__(self, "context_snapshot", dict(self.context_snapshot))


@dataclass(frozen=True, slots=True)
class LockView:
    """Portable summary of one item/field lock."""

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
        object.__setattr__(self, "field", self.field if isinstance(self.field, str) else "")
        object.__setattr__(self, "value", validate_field_value(self.value, "value"))
        object.__setattr__(
            self,
            "source_claim_id",
            ClaimId(self.source_claim_id) if self.source_claim_id else None,
        )
        object.__setattr__(self, "created_at", ensure_datetime(self.created_at, "created_at"))
        object.__setattr__(self, "updated_at", ensure_datetime(self.updated_at, "updated_at"))


@dataclass(frozen=True, slots=True)
class ReviewSessionView:
    """A review session and the corrections linked to it."""

    id: ReviewSessionId
    item_ids: tuple[ItemId, ...]
    fields: tuple[str, ...]
    started_at: datetime
    finished_at: datetime | None = None
    corrections: tuple[CorrectionId, ...] = ()
    notes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", require_id(self.id, "id"))
        object.__setattr__(self, "item_ids", tuple(self.item_ids))
        object.__setattr__(self, "fields", tuple(self.fields))
        object.__setattr__(self, "started_at", ensure_datetime(self.started_at, "started_at"))
        if self.finished_at is not None:
            object.__setattr__(
                self,
                "finished_at",
                ensure_datetime(self.finished_at, "finished_at"),
            )
        object.__setattr__(self, "corrections", tuple(self.corrections))
        object.__setattr__(self, "notes", tuple(self.notes))


@dataclass(frozen=True, slots=True)
class FeedbackExportResult:
    """Where an export was written and how many records it contains."""

    format: str
    output_path: Path
    event_count: int
    correction_count: int = 0
    jsonl_events: int = 0
    db_events: int = 0
    warnings: tuple[str, ...] = ()
    generated_at: datetime = dc_field(default_factory=utc_now)

    def __post_init__(self) -> None:
        object.__setattr__(self, "format", require_text(self.format, "format"))
        object.__setattr__(self, "output_path", Path(self.output_path))
        object.__setattr__(self, "warnings", tuple(self.warnings))
        object.__setattr__(self, "generated_at", ensure_datetime(self.generated_at, "generated_at"))

    @property
    def path(self) -> Path:
        return self.output_path

    @property
    def count(self) -> int:
        return self.event_count


@dataclass(frozen=True, slots=True)
class FeedbackGroupStats:
    """Counts and rates for one field/source/method group."""

    key: str
    total: int = 0
    reviews: int = 0
    corrections: int = 0
    approvals: int = 0
    unknowns: int = 0
    correction_rate: float = 0.0
    unknown_rate: float = 0.0

    def __post_init__(self) -> None:
        object.__setattr__(self, "key", require_text(self.key, "key"))
        for name in ("total", "reviews", "corrections", "approvals", "unknowns"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if not 0.0 <= float(self.correction_rate) <= 1.0:
            raise ValueError("correction_rate must be between 0 and 1")
        if not 0.0 <= float(self.unknown_rate) <= 1.0:
            raise ValueError("unknown_rate must be between 0 and 1")


@dataclass(frozen=True, slots=True)
class FeedbackStatsReport:
    """Aggregated feedback statistics; never claims accuracy without ground truth."""

    total_events: int = 0
    total_decisions: int = 0
    total_items: int = 0
    reviewed_items: int = 0
    total_reviews: int = 0
    corrections: int = 0
    approvals: int = 0
    rejections: int = 0
    locks: int = 0
    unlocks: int = 0
    review_notes: int = 0
    unknown_decisions: int = 0
    correction_rate: float = 0.0
    review_rate: float = 0.0
    unknown_rate: float = 0.0
    accuracy: float | None = None
    by_kind: dict[str, int] = dc_field(default_factory=dict)
    by_field: dict[str, FeedbackGroupStats] = dc_field(default_factory=dict)
    by_source: dict[str, FeedbackGroupStats] = dc_field(default_factory=dict)
    by_method: dict[str, FeedbackGroupStats] = dc_field(default_factory=dict)
    by_field_counts: dict[str, int] = dc_field(default_factory=dict)
    by_source_counts: dict[str, int] = dc_field(default_factory=dict)
    by_method_counts: dict[str, int] = dc_field(default_factory=dict)
    group_by: str = "field"
    groups: dict[str, FeedbackGroupStats] = dc_field(default_factory=dict)
    warnings: tuple[str, ...] = ()
    generated_at: datetime = dc_field(default_factory=utc_now)
    total: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "by_field", dict(self.by_field))
        object.__setattr__(self, "by_source", dict(self.by_source))
        object.__setattr__(self, "by_method", dict(self.by_method))
        object.__setattr__(self, "by_kind", dict(self.by_kind))
        object.__setattr__(self, "by_field_counts", dict(self.by_field_counts))
        object.__setattr__(self, "by_source_counts", dict(self.by_source_counts))
        object.__setattr__(self, "by_method_counts", dict(self.by_method_counts))
        object.__setattr__(self, "groups", dict(self.groups))
        object.__setattr__(self, "warnings", tuple(self.warnings))
        object.__setattr__(self, "generated_at", ensure_datetime(self.generated_at, "generated_at"))
        if self.group_by not in {"field", "source", "method"}:
            raise ValueError("group_by must be field, source, or method")

    @property
    def correction_count(self) -> int:
        return self.corrections

    @property
    def review_count(self) -> int:
        return self.total_reviews


__all__ = [
    "CorrectionView",
    "FeedbackExportResult",
    "FeedbackGroupStats",
    "FeedbackStatsReport",
    "LockView",
    "ReviewSessionView",
]
