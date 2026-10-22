"""Item aggregate root and its status state machine."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from trackforge.domain.errors import InvalidStateTransition
from trackforge.domain.model._validation import (
    coerce_enum,
    ensure_datetime,
    ensure_id_tuple,
    optional_enum,
    optional_id,
    require_id,
    utc_now,
)
from trackforge.domain.model.identity import ContentKind
from trackforge.domain.model.ids import (
    CollectionId,
    DecisionRecordId,
    IdentityHypothesisId,
    IntakeId,
    ItemId,
    MediaId,
    RenderId,
)


class ItemStatus(StrEnum):
    DISCOVERED = "discovered"
    ACQUIRED = "acquired"
    OBSERVED = "observed"
    INTERPRETED = "interpreted"
    RESOLVED = "resolved"
    RENDERED = "rendered"
    PUBLISHED = "published"
    NEEDS_REVIEW = "needs_review"
    FAILED = "failed"
    DUPLICATE = "duplicate"
    ARCHIVED = "archived"

    def can_transition_to(self, target: ItemStatus | str) -> bool:
        """Return whether moving to ``target`` is legal without an explicit reset."""

        target_status = coerce_enum(target, ItemStatus, "status")
        return target_status in ALLOWED_TRANSITIONS[self]

    def is_terminal(self) -> bool:
        return self is ItemStatus.ARCHIVED


_ACTIVE_STATUSES = frozenset(
    {
        ItemStatus.DISCOVERED,
        ItemStatus.ACQUIRED,
        ItemStatus.OBSERVED,
        ItemStatus.INTERPRETED,
        ItemStatus.RESOLVED,
        ItemStatus.RENDERED,
    }
)

_LINEAR_TRANSITIONS: dict[ItemStatus, frozenset[ItemStatus]] = {
    ItemStatus.DISCOVERED: frozenset({ItemStatus.ACQUIRED}),
    ItemStatus.ACQUIRED: frozenset({ItemStatus.OBSERVED}),
    ItemStatus.OBSERVED: frozenset({ItemStatus.INTERPRETED}),
    ItemStatus.INTERPRETED: frozenset({ItemStatus.RESOLVED}),
    ItemStatus.RESOLVED: frozenset({ItemStatus.RENDERED}),
    ItemStatus.RENDERED: frozenset({ItemStatus.PUBLISHED}),
}


def _allowed_transitions(status: ItemStatus) -> frozenset[ItemStatus]:
    allowed = set(_LINEAR_TRANSITIONS.get(status, frozenset()))
    if status is not ItemStatus.ARCHIVED:
        allowed.add(ItemStatus.ARCHIVED)
    if status in _ACTIVE_STATUSES:
        allowed.update(
            {
                ItemStatus.NEEDS_REVIEW,
                ItemStatus.FAILED,
                ItemStatus.DUPLICATE,
            }
        )
    if status is ItemStatus.NEEDS_REVIEW:
        allowed.add(ItemStatus.FAILED)
    return frozenset(allowed)


ALLOWED_TRANSITIONS: dict[ItemStatus, frozenset[ItemStatus]] = {
    status: _allowed_transitions(status) for status in ItemStatus
}


@dataclass(slots=True)
class Item:
    """Aggregate root that references media, intakes, claims, and renders by ID."""

    id: ItemId
    media_id: MediaId | None = None
    primary_intake_id: IntakeId | None = None
    collection_id: CollectionId | None = None
    kind: ContentKind | None = None
    status: ItemStatus = ItemStatus.DISCOVERED
    identity_hypothesis_ids: tuple[IdentityHypothesisId, ...] = ()
    decision_record_id: DecisionRecordId | None = None
    render_ids: tuple[RenderId, ...] = ()
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime | None = None

    def __setattr__(self, name: str, value: object) -> None:
        """Validate status assignments and keep ``updated_at`` coherent."""

        if name != "status":
            object.__setattr__(self, name, value)
            return

        target = coerce_enum(value, ItemStatus, "status")
        current = getattr(self, "status", None)
        if isinstance(current, ItemStatus) and target is not current:
            if not current.can_transition_to(target):
                raise InvalidStateTransition(
                    f"cannot transition item from {current.value} to {target.value}",
                    context={
                        "item_id": getattr(self, "id", ""),
                        "from_status": current.value,
                        "to_status": target.value,
                    },
                )
            changed_at = utc_now()
            created_at = getattr(self, "created_at", None)
            if isinstance(created_at, datetime) and changed_at < created_at:
                changed_at = created_at
            object.__setattr__(self, "updated_at", changed_at)
        object.__setattr__(self, name, target)

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", require_id(self.id, "id"))
        object.__setattr__(self, "media_id", optional_id(self.media_id, "media_id"))
        object.__setattr__(
            self,
            "primary_intake_id",
            optional_id(self.primary_intake_id, "primary_intake_id"),
        )
        object.__setattr__(
            self,
            "collection_id",
            optional_id(self.collection_id, "collection_id"),
        )
        object.__setattr__(self, "kind", optional_enum(self.kind, ContentKind, "kind"))
        object.__setattr__(
            self,
            "status",
            coerce_enum(self.status, ItemStatus, "status"),
        )
        object.__setattr__(
            self,
            "identity_hypothesis_ids",
            ensure_id_tuple(self.identity_hypothesis_ids, "identity_hypothesis_ids"),
        )
        object.__setattr__(
            self,
            "decision_record_id",
            optional_id(self.decision_record_id, "decision_record_id"),
        )
        object.__setattr__(self, "render_ids", ensure_id_tuple(self.render_ids, "render_ids"))
        object.__setattr__(
            self,
            "created_at",
            ensure_datetime(self.created_at, "created_at"),
        )
        if self.updated_at is None:
            object.__setattr__(self, "updated_at", self.created_at)
        else:
            object.__setattr__(
                self,
                "updated_at",
                ensure_datetime(self.updated_at, "updated_at"),
            )
            if self.updated_at < self.created_at:
                raise InvalidStateTransition(
                    "updated_at must not be before created_at",
                    context={
                        "created_at": self.created_at.isoformat(),
                        "updated_at": self.updated_at.isoformat(),
                    },
                )

    def transition_to(
        self,
        status: ItemStatus | str,
        *,
        at: datetime | None = None,
    ) -> Item:
        """Return a new item in ``status`` or raise for an illegal transition.

        Going backwards requires an explicit reset operation, which is outside
        this plan.  Re-applying the current status is a no-op.
        """

        target = coerce_enum(status, ItemStatus, "status")
        if target is self.status:
            return self
        if not self.status.can_transition_to(target):
            raise InvalidStateTransition(
                f"cannot transition item from {self.status.value} to {target.value}",
                context={
                    "item_id": self.id,
                    "from_status": self.status.value,
                    "to_status": target.value,
                },
            )
        changed_at = utc_now() if at is None else ensure_datetime(at, "at")
        if changed_at < self.created_at:
            raise InvalidStateTransition(
                "transition timestamp must not be before created_at",
                context={
                    "created_at": self.created_at.isoformat(),
                    "updated_at": changed_at.isoformat(),
                },
            )
        object.__setattr__(self, "status", target)
        object.__setattr__(self, "updated_at", changed_at)
        return self

    # Readable aliases used by application code.
    def transition(
        self,
        status: ItemStatus | str,
        *,
        at: datetime | None = None,
    ) -> Item:
        return self.transition_to(status, at=at)

    def advance_to(
        self,
        status: ItemStatus | str,
        *,
        at: datetime | None = None,
    ) -> Item:
        return self.transition_to(status, at=at)


__all__ = ["ALLOWED_TRANSITIONS", "Item", "ItemStatus"]
