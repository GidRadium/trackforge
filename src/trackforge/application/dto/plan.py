"""Read-only DTOs for planning and local acquisition."""

from __future__ import annotations

from dataclasses import dataclass, field

from trackforge.domain.model.capabilities import Cost
from trackforge.domain.model.ids import FileInstanceId, IntakeId, ItemId, MediaId
from trackforge.domain.model.item import ItemStatus
from trackforge.domain.model.source import Candidate, SourceRef


@dataclass(frozen=True, slots=True)
class PlanOptions:
    """Options that influence source resolution and the observation plan."""

    profile: str = "balanced"
    enabled: tuple[str, ...] = ()
    disabled: tuple[str, ...] = ()
    offline: bool = False


@dataclass(frozen=True, slots=True)
class PlanStepView:
    """One user-visible step in a :class:`PlanView`."""

    node_id: str
    capability_id: str
    description: str
    requires: tuple[str, ...] = ()
    provides: tuple[str, ...] = ()
    estimated_cost: Cost = field(default_factory=Cost)


@dataclass(frozen=True, slots=True)
class PlanView:
    """A side-effect-free description of what acquiring and observing would do."""

    source: SourceRef
    candidates: tuple[Candidate, ...] = ()
    steps: tuple[PlanStepView, ...] = ()
    estimated_cost: Cost = field(default_factory=Cost)
    warnings: tuple[str, ...] = ()
    blocked_reason: str | None = None
    resolver_id: str | None = None

    @property
    def blocked(self) -> bool:
        return self.blocked_reason is not None


@dataclass(frozen=True, slots=True)
class AcquireOptions:
    """Options for one acquisition attempt."""

    source_snapshot_ref: str | None = None
    format_id: str | None = None
    offline: bool = False
    force: bool = False


@dataclass(frozen=True, slots=True)
class ItemView:
    """Result of copying one candidate into the catalog and CAS."""

    item_id: ItemId
    status: ItemStatus
    media_id: MediaId
    intake_id: IntakeId
    file_instance_id: FileInstanceId
    content_hash: str
    size_bytes: int
    cas_path: str
    source_path: str
    reused_media: bool = False
    warnings: tuple[str, ...] = ()


__all__ = [
    "AcquireOptions",
    "ItemView",
    "PlanOptions",
    "PlanStepView",
    "PlanView",
]
