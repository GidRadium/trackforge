"""Feedback events, corrections, and review sessions."""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field as dc_field
from datetime import datetime
from enum import StrEnum
from typing import Any

from trackforge.domain.errors import DomainValidationError, UnsupportedScope
from trackforge.domain.model._validation import (
    FieldValue,
    coerce_enum,
    ensure_bool,
    ensure_datetime,
    ensure_id_tuple,
    ensure_str_tuple,
    json_mapping,
    optional_id,
    optional_text,
    require_id,
    require_text,
    utc_now,
    validate_field_value,
)
from trackforge.domain.model.ids import (
    ClaimId,
    CorrectionId,
    DecisionId,
    FeedbackEventId,
    ItemId,
    ReviewSessionId,
)

_UNSET: Any = object()


class FeedbackKind(StrEnum):
    CORRECTION = "correction"
    APPROVAL = "approval"
    REJECTION = "rejection"
    LOCK = "lock"
    UNLOCK = "unlock"
    REVIEW_NOTE = "review_note"
    EXTERNAL_EDIT = "external_edit"


class CorrectionScope(StrEnum):
    ITEM = "item"
    SOURCE = "source"
    ARTIST = "artist"
    PROFILE = "profile"
    GLOBAL = "global"

    @property
    def executable_in_v1(self) -> bool:
        return self is CorrectionScope.ITEM


class ReasonCode(StrEnum):
    WRONG_TITLE = "wrong_title"
    WRONG_ARTIST = "wrong_artist"
    WRONG_ALBUM = "wrong_album"
    WRONG_COVER = "wrong_cover"
    WRONG_TYPE = "wrong_type"
    WRONG_LYRICS = "wrong_lyrics"
    WRONG_DATE = "wrong_date"
    OTHER = "other"


@dataclass(frozen=True, slots=True)
class FeedbackEvent:
    """A lightweight, append-only feedback signal."""

    id: FeedbackEventId
    kind: FeedbackKind
    item_id: ItemId
    field: str | None = None
    system_value: FieldValue = None
    user_value: FieldValue = None
    scope: CorrectionScope | None = None
    reason: str | None = None
    context_snapshot: dict[str, Any] | None = None
    created_at: datetime = dc_field(default_factory=utc_now)
    system_decision_id: DecisionId | None = None
    user_claim_id: ClaimId | None = None
    review_session_id: ReviewSessionId | None = None
    reason_code: ReasonCode | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", require_id(self.id, "id"))
        object.__setattr__(self, "kind", coerce_enum(self.kind, FeedbackKind, "kind"))
        object.__setattr__(self, "item_id", require_id(self.item_id, "item_id"))
        object.__setattr__(self, "field", optional_text(self.field, "field"))
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
            "scope",
            coerce_enum(self.scope, CorrectionScope, "scope") if self.scope is not None else None,
        )
        object.__setattr__(self, "reason", optional_text(self.reason, "reason"))
        if self.context_snapshot is not None:
            object.__setattr__(
                self,
                "context_snapshot",
                json_mapping(self.context_snapshot, "context_snapshot"),
            )
        object.__setattr__(
            self,
            "system_decision_id",
            optional_id(self.system_decision_id, "system_decision_id"),
        )
        object.__setattr__(
            self,
            "user_claim_id",
            optional_id(self.user_claim_id, "user_claim_id"),
        )
        object.__setattr__(
            self,
            "review_session_id",
            optional_id(self.review_session_id, "review_session_id"),
        )
        object.__setattr__(
            self,
            "reason_code",
            (
                coerce_enum(self.reason_code, ReasonCode, "reason_code")
                if self.reason_code is not None
                else None
            ),
        )
        object.__setattr__(
            self,
            "created_at",
            ensure_datetime(self.created_at, "created_at"),
        )


@dataclass(frozen=True, slots=True)
class Correction:
    """A user correction of a system decision, with snapshot context."""

    id: CorrectionId
    item_id: ItemId
    field: str
    system_decision_id: DecisionId
    system_value: FieldValue
    user_value: FieldValue
    user_claim_id: ClaimId | None = None
    scope: CorrectionScope = CorrectionScope.ITEM
    reason_code: ReasonCode | str = ""
    reason_text: str | None = None
    context_snapshot: dict[str, Any] = _UNSET
    created_at: datetime = dc_field(default_factory=utc_now)
    applied: bool = False
    review_session_id: ReviewSessionId | None = None

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
        if self.context_snapshot is _UNSET:
            raise DomainValidationError(
                "correction context_snapshot is required",
                context={"field": self.field},
            )
        object.__setattr__(
            self,
            "context_snapshot",
            json_mapping(self.context_snapshot, "context_snapshot"),
        )
        scope = coerce_enum(self.scope, CorrectionScope, "scope")
        if not scope.executable_in_v1:
            raise UnsupportedScope(
                "corrections with source/artist/profile/global scope are not "
                "executable in v1; use a PreferenceRule when it is available",
                context={"scope": scope.value, "field": self.field},
            )
        object.__setattr__(self, "scope", scope)
        object.__setattr__(
            self,
            "user_claim_id",
            optional_id(self.user_claim_id, "user_claim_id"),
        )
        object.__setattr__(
            self,
            "reason_text",
            optional_text(self.reason_text, "reason_text"),
        )
        object.__setattr__(
            self,
            "created_at",
            ensure_datetime(self.created_at, "created_at"),
        )
        object.__setattr__(self, "applied", ensure_bool(self.applied, "applied"))
        object.__setattr__(
            self,
            "review_session_id",
            optional_id(self.review_session_id, "review_session_id"),
        )


@dataclass(frozen=True, slots=True)
class ReviewSessionItem:
    """Association between a review session and one item."""

    session_id: ReviewSessionId
    item_id: ItemId

    def __post_init__(self) -> None:
        object.__setattr__(self, "session_id", require_id(self.session_id, "session_id"))
        object.__setattr__(self, "item_id", require_id(self.item_id, "item_id"))


@dataclass(frozen=True, slots=True)
class ReviewSession:
    """One review interaction over a set of items and fields."""

    id: ReviewSessionId
    item_ids: tuple[ItemId, ...]
    fields: tuple[str, ...]
    started_at: datetime
    finished_at: datetime | None = None
    corrections: tuple[CorrectionId, ...] = ()
    notes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", require_id(self.id, "id"))
        object.__setattr__(self, "item_ids", ensure_id_tuple(self.item_ids, "item_ids"))
        object.__setattr__(self, "fields", ensure_str_tuple(self.fields, "fields"))
        object.__setattr__(
            self,
            "started_at",
            ensure_datetime(self.started_at, "started_at"),
        )
        if self.finished_at is not None:
            finished_at = ensure_datetime(self.finished_at, "finished_at")
            if finished_at < self.started_at:
                raise DomainValidationError(
                    "finished_at must not be before started_at",
                    context={
                        "started_at": self.started_at.isoformat(),
                        "finished_at": finished_at.isoformat(),
                    },
                )
            object.__setattr__(self, "finished_at", finished_at)
        object.__setattr__(
            self,
            "corrections",
            ensure_id_tuple(self.corrections, "corrections"),
        )
        object.__setattr__(self, "notes", ensure_str_tuple(self.notes, "notes"))


__all__ = [
    "Correction",
    "CorrectionScope",
    "FeedbackEvent",
    "FeedbackKind",
    "ReasonCode",
    "ReviewSession",
    "ReviewSessionItem",
]
