"""Application port for the append-only feedback journal.

The JSONL journal is the export-friendly source of truth, while SQLite is the
query index.  Review writes both through this port and checks for divergence
without deleting history.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from trackforge.domain.model.feedback import Correction, FeedbackEvent
from trackforge.domain.model.ids import CorrectionId, ItemId


@dataclass(frozen=True, slots=True)
class FeedbackConsistency:
    """Read-only comparison of JSONL and DB event ids."""

    jsonl_events: int = 0
    db_events: int = 0
    missing_in_jsonl: tuple[str, ...] = ()
    missing_in_db: tuple[str, ...] = ()
    duplicate_jsonl_ids: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "missing_in_jsonl", tuple(self.missing_in_jsonl))
        object.__setattr__(self, "missing_in_db", tuple(self.missing_in_db))
        object.__setattr__(
            self,
            "duplicate_jsonl_ids",
            tuple(self.duplicate_jsonl_ids),
        )
        object.__setattr__(self, "warnings", tuple(self.warnings))

    @property
    def consistent(self) -> bool:
        return not (self.missing_in_jsonl or self.missing_in_db or self.duplicate_jsonl_ids)


@runtime_checkable
class FeedbackJournal(Protocol):
    """Append and read feedback JSONL/correction snapshots."""

    @property
    def events_path(self) -> Path | None: ...

    @property
    def corrections_dir(self) -> Path | None: ...

    async def append_event(
        self,
        event: FeedbackEvent,
        *,
        correction: Correction | None = None,
        applied: bool = True,
    ) -> None: ...

    async def write_correction(self, correction: Correction) -> None: ...

    async def read_events(
        self,
        *,
        item_id: ItemId | None = None,
        field: str | None = None,
        since: datetime | None = None,
    ) -> list[FeedbackEvent]: ...

    async def read_event_documents(
        self,
        *,
        item_id: ItemId | None = None,
        field: str | None = None,
        since: datetime | None = None,
    ) -> list[dict[str, Any]]: ...

    async def get_correction(self, correction_id: CorrectionId | str) -> Correction | None: ...

    async def list_corrections(
        self,
        *,
        item_id: ItemId | None = None,
        field: str | None = None,
    ) -> list[Correction]: ...

    async def consistency(self, events: list[FeedbackEvent]) -> FeedbackConsistency: ...


@dataclass(frozen=True, slots=True)
class NullFeedbackJournal:
    """A no-op journal for tests/callers that only configured a database."""

    warnings: tuple[str, ...] = field(
        default=("feedback JSONL journal is not configured; DB events only",),
    )

    @property
    def events_path(self) -> Path | None:
        return None

    @property
    def corrections_dir(self) -> Path | None:
        return None

    async def append_event(
        self,
        event: FeedbackEvent,
        *,
        correction: Correction | None = None,
        applied: bool = True,
    ) -> None:
        return None

    async def write_correction(self, correction: Correction) -> None:
        return None

    async def read_events(
        self,
        *,
        item_id: ItemId | None = None,
        field: str | None = None,
        since: datetime | None = None,
    ) -> list[FeedbackEvent]:
        return []

    async def read_event_documents(
        self,
        *,
        item_id: ItemId | None = None,
        field: str | None = None,
        since: datetime | None = None,
    ) -> list[dict[str, Any]]:
        return []

    async def get_correction(self, correction_id: CorrectionId | str) -> Correction | None:
        return None

    async def list_corrections(
        self,
        *,
        item_id: ItemId | None = None,
        field: str | None = None,
    ) -> list[Correction]:
        return []

    async def consistency(self, events: list[FeedbackEvent]) -> FeedbackConsistency:
        return FeedbackConsistency()


# Compatibility aliases for callers that use store/writer terminology.
FeedbackStore = FeedbackJournal
FeedbackWriter = FeedbackJournal


__all__ = [
    "FeedbackConsistency",
    "FeedbackJournal",
    "FeedbackStore",
    "FeedbackWriter",
    "NullFeedbackJournal",
]
