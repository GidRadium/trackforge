"""Collections: ordered groups of source candidates and their context.

A collection is an input container (playlist page, album page, local folder),
not a track and not a release.  Its context claims are evidence available to
tagging policy; they are deliberately separate from recording claims.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from trackforge.domain.errors import DomainValidationError
from trackforge.domain.model._validation import (
    Array,
    FieldValue,
    coerce_enum,
    ensure_datetime,
    ensure_int,
    ensure_sequence,
    ensure_str_tuple,
    optional_id,
    optional_text,
    require_id,
    require_text,
    utc_now,
    validate_confidence,
    validate_field_value,
)
from trackforge.domain.model.ids import CollectionId, ItemId
from trackforge.domain.model.source import (
    Candidate,
    CollectionKind,
    CollectionRef,
    LocalPath,
    SourceRef,
    TextQuery,
    Url,
)

_PATH_SEGMENT_RE = re.compile(r"[\\/]+")


class TagBasis(StrEnum):
    """Which context wins for album-oriented tags."""

    ALBUM_FIRST = "album_first"
    PLAYLIST_FIRST = "playlist_first"
    ASK = "ask"


class CollectionItemStatus(StrEnum):
    """Persisted progress of one collection item; the source of resume truth."""

    PLANNED = "planned"
    ACQUIRED = "acquired"
    PROCESSED = "processed"
    SKIPPED = "skipped"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class CollectionContextClaim:
    """A collection-level hypothesis such as album, owner, date, or cover."""

    field: str
    value: FieldValue
    source: str = "collection"
    confidence: float = 0.5
    evidence: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "field", require_text(self.field, "field"))
        object.__setattr__(self, "value", validate_field_value(self.value, "value"))
        object.__setattr__(self, "source", require_text(self.source, "source"))
        object.__setattr__(
            self,
            "confidence",
            validate_confidence(self.confidence, "confidence"),
        )
        object.__setattr__(self, "evidence", ensure_str_tuple(self.evidence, "evidence"))


@dataclass(frozen=True, slots=True)
class CollectionItem:
    """One ordered candidate inside a collection."""

    position: int
    candidate: Candidate
    status: CollectionItemStatus = CollectionItemStatus.PLANNED
    item_id: ItemId | None = None
    error: str | None = None
    updated_at: datetime | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "position",
            ensure_int(self.position, "position", minimum=1),
        )
        if not isinstance(self.candidate, Candidate):
            raise DomainValidationError("candidate must be a Candidate")
        object.__setattr__(
            self,
            "status",
            coerce_enum(self.status, CollectionItemStatus, "status"),
        )
        object.__setattr__(self, "item_id", optional_id(self.item_id, "item_id"))
        object.__setattr__(self, "error", optional_text(self.error, "error"))
        if self.updated_at is not None:
            object.__setattr__(
                self,
                "updated_at",
                ensure_datetime(self.updated_at, "updated_at"),
            )
        if (
            self.status in {CollectionItemStatus.ACQUIRED, CollectionItemStatus.PROCESSED}
            and self.item_id is None
        ):
            raise DomainValidationError(
                f"collection item in status {self.status.value} requires item_id",
                context={"position": self.position},
            )

    @property
    def external_id(self) -> str:
        return self.candidate.external_id


@dataclass(frozen=True, slots=True)
class Collection:
    """A source container plus its ordered candidate items."""

    id: CollectionId
    kind: CollectionKind
    source_ref: SourceRef
    title: str
    owner: str | None = None
    description: str | None = None
    context_claims: tuple[CollectionContextClaim, ...] = ()
    items: tuple[CollectionItem, ...] = ()
    tag_basis: TagBasis = TagBasis.ALBUM_FIRST
    created_at: datetime = field(default_factory=utc_now)
    warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", require_id(self.id, "id"))
        object.__setattr__(self, "kind", coerce_enum(self.kind, CollectionKind, "kind"))
        if not isinstance(self.source_ref, (LocalPath, Url, TextQuery, CollectionRef)):
            raise DomainValidationError("source_ref must be a SourceRef")
        object.__setattr__(self, "title", validate_collection_title(self.title))
        object.__setattr__(self, "owner", optional_text(self.owner, "owner"))
        object.__setattr__(
            self,
            "description",
            optional_text(self.description, "description"),
        )

        raw_claims = ensure_sequence(self.context_claims, "context_claims")
        claims: list[CollectionContextClaim] = []
        for index, claim in enumerate(raw_claims):
            if not isinstance(claim, CollectionContextClaim):
                raise DomainValidationError(
                    f"context_claims[{index}] must be a CollectionContextClaim"
                )
            claims.append(claim)
        object.__setattr__(self, "context_claims", Array(claims))

        raw_items = ensure_sequence(self.items, "items")
        items: list[CollectionItem] = []
        seen: set[int] = set()
        for index, item in enumerate(raw_items):
            if not isinstance(item, CollectionItem):
                raise DomainValidationError(f"items[{index}] must be a CollectionItem")
            if item.position in seen:
                raise DomainValidationError(
                    "collection positions must be unique",
                    context={"position": item.position},
                )
            seen.add(item.position)
            items.append(item)
        if [item.position for item in items] != sorted(item.position for item in items):
            raise DomainValidationError("collection items must be ordered by position")
        object.__setattr__(self, "items", Array(items))

        object.__setattr__(
            self,
            "tag_basis",
            coerce_enum(self.tag_basis, TagBasis, "tag_basis"),
        )
        object.__setattr__(
            self,
            "created_at",
            ensure_datetime(self.created_at, "created_at"),
        )
        object.__setattr__(self, "warnings", ensure_str_tuple(self.warnings, "warnings"))

    def item_at(self, position: int) -> CollectionItem | None:
        return next((item for item in self.items if item.position == position), None)

    def context_value(self, *fields: str) -> FieldValue:
        wanted = {field.strip().casefold() for field in fields}
        for claim in reversed(self.context_claims):
            if claim.field.casefold() in wanted and claim.value is not None:
                return claim.value
        return None


def validate_collection_title(value: object) -> str:
    """Validate a collection title before it can become a library folder."""

    text = require_text(value, "title")
    if "\x00" in text:
        raise DomainValidationError(
            "collection title must not contain NUL bytes",
            context={"field": "title"},
        )
    segments = [segment for segment in _PATH_SEGMENT_RE.split(text) if segment]
    if (
        text.startswith(("/", "\\"))
        or not segments
        or any(segment in {".", ".."} for segment in segments)
    ):
        raise DomainValidationError(
            "collection title must not contain path traversal",
            user_action="Choose a collection title that stays inside the library.",
            context={"field": "title", "value": text},
        )
    return text


__all__ = [
    "Collection",
    "CollectionContextClaim",
    "CollectionItem",
    "CollectionItemStatus",
    "CollectionKind",
    "TagBasis",
    "validate_collection_title",
]
