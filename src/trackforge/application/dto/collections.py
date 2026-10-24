"""DTOs for collection planning, processing, and resume."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from trackforge.domain.errors import DomainValidationError
from trackforge.domain.model._validation import (
    coerce_enum,
    ensure_int,
    optional_text,
)
from trackforge.domain.model.capabilities import Cost
from trackforge.domain.model.collection import (
    Collection,
    CollectionContextClaim,
    CollectionItem,
    CollectionItemStatus,
    CollectionKind,
    TagBasis,
)
from trackforge.domain.model.ids import CollectionId, ItemId
from trackforge.domain.model.source import Candidate, SourceRef

MAX_COLLECTION_ITEMS = 500


@dataclass(frozen=True, slots=True)
class CollectionPlanOptions:
    """User selection, basis, and the hard collection-size cap."""

    start: int | None = None
    end: int | None = None
    items: str | tuple[int, ...] = ()
    skip: str | tuple[int, ...] = ()
    tag_basis: TagBasis | str = TagBasis.ALBUM_FIRST
    max_items: int = MAX_COLLECTION_ITEMS
    offline: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.offline, bool):
            raise DomainValidationError("offline must be a boolean")
        for name in ("start", "end"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(
                    self,
                    name,
                    ensure_int(value, name, minimum=1),
                )
        if self.start is not None and self.end is not None and self.start > self.end:
            raise DomainValidationError(
                "playlist start must not be greater than playlist end",
                context={"start": self.start, "end": self.end},
            )
        object.__setattr__(self, "items", parse_positions(self.items, "playlist_items"))
        object.__setattr__(self, "skip", parse_positions(self.skip, "skip"))
        object.__setattr__(
            self,
            "tag_basis",
            coerce_enum(self.tag_basis, TagBasis, "tag_basis"),
        )
        object.__setattr__(
            self,
            "max_items",
            ensure_int(self.max_items, "max_items", minimum=1, maximum=MAX_COLLECTION_ITEMS),
        )

    @property
    def has_range_or_item_filter(self) -> bool:
        return self.start is not None or self.end is not None or bool(self.items)

    def source_options(self) -> dict[str, Any]:
        """Options passed to an enumeration adapter for early filtering."""

        return {
            "playlist_start": self.start,
            "playlist_end": self.end,
            "playlist_items": tuple(str(item) for item in self.items),
            "offline": self.offline,
        }


@dataclass(frozen=True, slots=True)
class CollectionItemContext:
    """Collection context carried into one item's pipeline run."""

    collection_id: CollectionId
    kind: CollectionKind
    title: str
    position: int
    tag_basis: TagBasis = TagBasis.ALBUM_FIRST
    owner: str | None = None
    context_claims: tuple[CollectionContextClaim, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "kind", coerce_enum(self.kind, CollectionKind, "kind"))
        object.__setattr__(
            self,
            "tag_basis",
            coerce_enum(self.tag_basis, TagBasis, "tag_basis"),
        )
        object.__setattr__(self, "position", ensure_int(self.position, "position", minimum=1))
        object.__setattr__(self, "owner", optional_text(self.owner, "owner"))

    @classmethod
    def from_collection(
        cls,
        collection: Collection,
        item: CollectionItem,
    ) -> CollectionItemContext:
        return cls(
            collection_id=collection.id,
            kind=collection.kind,
            title=collection.title,
            position=item.position,
            tag_basis=collection.tag_basis,
            owner=collection.owner,
            context_claims=collection.context_claims,
        )


@dataclass(frozen=True, slots=True)
class CollectionPlanView:
    """Side-effect-free description of what a collection add would process."""

    collection: Collection
    selected_items: tuple[CollectionItem, ...]
    estimated_cost: Cost = field(default_factory=Cost)
    warnings: tuple[str, ...] = ()
    limits: dict[str, int] = field(default_factory=dict)
    blocked_reason: str | None = None
    resolver_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "selected_items", tuple(self.selected_items))
        object.__setattr__(self, "warnings", tuple(self.warnings))
        object.__setattr__(self, "limits", dict(self.limits))

    @property
    def tag_basis(self) -> TagBasis:
        return self.collection.tag_basis

    @property
    def items(self) -> tuple[CollectionItem, ...]:
        """Alias for the selected items."""

        return self.selected_items

    @property
    def source(self) -> SourceRef:
        """PlanView-style source reference."""

        return self.collection.source_ref

    @property
    def candidates(self) -> tuple[Candidate, ...]:
        """PlanView-style candidates for the selected positions."""

        return tuple(item.candidate for item in self.selected_items)

    @property
    def blocked(self) -> bool:
        return self.blocked_reason is not None

    @property
    def collection_id(self) -> CollectionId:
        return self.collection.id

    @property
    def selected_positions(self) -> tuple[int, ...]:
        return tuple(item.position for item in self.selected_items)


class CollectionRunStatus(StrEnum):
    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class CollectionRunItem:
    """Outcome for one collection position."""

    position: int
    status: CollectionItemStatus
    item_id: ItemId | None = None
    output_path: str | None = None
    error: str | None = None
    warnings: tuple[str, ...] = ()
    cached: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "status",
            coerce_enum(self.status, CollectionItemStatus, "status"),
        )
        object.__setattr__(self, "warnings", tuple(self.warnings))


@dataclass(frozen=True, slots=True)
class CollectionRunResult:
    """Observable result of adding or resuming a collection."""

    collection_id: CollectionId
    status: CollectionRunStatus
    items: tuple[CollectionRunItem, ...] = ()
    warnings: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()
    output_paths: tuple[str, ...] = ()
    processed: int = 0
    failed: int = 0
    skipped: int = 0
    remaining: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "status",
            coerce_enum(self.status, CollectionRunStatus, "status"),
        )
        object.__setattr__(self, "items", tuple(self.items))
        object.__setattr__(self, "warnings", tuple(self.warnings))
        object.__setattr__(self, "errors", tuple(self.errors))
        object.__setattr__(self, "output_paths", tuple(self.output_paths))

    @property
    def succeeded(self) -> bool:
        return self.status is CollectionRunStatus.COMPLETED


def parse_positions(
    value: str | int | tuple[int, ...] | list[int] | None,
    field_name: str = "positions",
) -> tuple[int, ...]:
    """Parse ``1,3-5`` (or an int sequence) into sorted unique positions."""

    if value is None or value == "":
        return ()
    raw: list[str] = []
    if isinstance(value, int) and not isinstance(value, bool):
        raw = [str(value)]
    elif isinstance(value, str):
        raw = [part.strip() for part in value.split(",") if part.strip()]
    elif isinstance(value, (list, tuple, set, frozenset)):
        raw = [str(part).strip() for part in value if str(part).strip()]
    else:
        raise DomainValidationError(
            f"{field_name} must be a comma-separated list or a sequence of positions",
            context={"field": field_name, "actual_type": type(value).__name__},
        )

    result: set[int] = set()
    for part in raw:
        if "-" in part:
            head, _, tail = part.partition("-")
            try:
                start, end = int(head.strip()), int(tail.strip())
            except ValueError:
                raise DomainValidationError(
                    f"{field_name} contains an invalid range",
                    context={"field": field_name, "value": part},
                ) from None
            if start < 1 or end < 1 or start > end:
                raise DomainValidationError(
                    f"{field_name} ranges must be positive and ordered",
                    context={"field": field_name, "value": part},
                )
            result.update(range(start, end + 1))
        else:
            try:
                position = int(part)
            except ValueError:
                raise DomainValidationError(
                    f"{field_name} contains a non-integer position",
                    context={"field": field_name, "value": part},
                ) from None
            if position < 1:
                raise DomainValidationError(
                    f"{field_name} positions must be positive",
                    context={"field": field_name, "value": part},
                )
            result.add(position)
    return tuple(sorted(result))


__all__ = [
    "MAX_COLLECTION_ITEMS",
    "CollectionItemContext",
    "CollectionPlanOptions",
    "CollectionPlanView",
    "CollectionRunItem",
    "CollectionRunResult",
    "CollectionRunStatus",
    "parse_positions",
]
