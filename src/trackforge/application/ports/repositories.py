"""Repository and unit-of-work ports for the operational catalog.

The application layer depends only on these protocols.  Concrete SQLite
adapters live in :mod:`trackforge.adapters.persistence.sqlite`; the domain
never imports SQLAlchemy or pydantic.

All repository methods are asynchronous.  Adapters run their synchronous
SQLAlchemy work in a worker thread so callers never block the event loop.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from types import TracebackType
from typing import Any, Protocol, runtime_checkable

from trackforge.application.dto.db import (
    ArtifactRecord,
    ClaimEventRecord,
    FeedbackStats,
    LockRecord,
    RunItemRecord,
    RunItemStatus,
    RunRecord,
    RunStatus,
    StepRecord,
    StepStatus,
)
from trackforge.application.ports.models import ModelRegistry
from trackforge.domain.model._validation import FieldValue
from trackforge.domain.model.claims import Claim, ClaimStatus
from trackforge.domain.model.decisions import Decision, DecisionRecord
from trackforge.domain.model.feedback import FeedbackEvent, FeedbackKind, ReviewSession
from trackforge.domain.model.ids import (
    ArtifactId,
    ClaimId,
    DecisionRecordId,
    FileInstanceId,
    IntakeId,
    ItemId,
    LibraryEntryId,
    MediaId,
    ReviewSessionId,
    RunId,
    StepId,
)
from trackforge.domain.model.item import Item, ItemStatus
from trackforge.domain.model.media import FileInstance, Intake, MediaObject
from trackforge.domain.model.outputs import (
    LibraryEntry,
    LibraryStatus,
    OutputBundle,
    Render,
)


@runtime_checkable
class ItemRepository(Protocol):
    """Persist the item aggregate root."""

    async def get(self, item_id: ItemId) -> Item | None: ...

    async def save(self, item: Item) -> Item: ...

    async def update_status(
        self,
        item_id: ItemId,
        status: ItemStatus | str,
        *,
        at: datetime | None = None,
    ) -> Item: ...

    async def find_by_media(self, media_id: MediaId) -> list[Item]: ...

    async def list_by_status(
        self,
        status: ItemStatus | str,
        *,
        limit: int | None = None,
    ) -> list[Item]: ...

    async def count_items(self) -> int: ...


@runtime_checkable
class MediaRepository(Protocol):
    """Persist immutable media objects."""

    async def get(self, media_id: MediaId) -> MediaObject | None: ...

    async def find_by_hash(self, content_hash: str) -> MediaObject | None: ...

    async def save(self, media: MediaObject) -> MediaObject: ...

    async def update_probe(
        self,
        media_id: MediaId,
        *,
        probe_artifact_id: ArtifactId,
        codec: str | None,
        bitrate: int | None,
        sample_rate: int | None,
        channels: int | None,
        duration_seconds: float | None,
    ) -> MediaObject: ...


@runtime_checkable
class FileInstanceRepository(Protocol):
    """Persist concrete appearances of media bytes on disk."""

    async def get(self, file_instance_id: FileInstanceId) -> FileInstance | None: ...

    async def find_by_media(self, media_id: MediaId) -> list[FileInstance]: ...

    async def find_by_path(self, path: str) -> list[FileInstance]: ...

    async def save(self, instance: FileInstance) -> FileInstance: ...


@runtime_checkable
class IntakeRepository(Protocol):
    """Persist acquisition facts."""

    async def get(self, intake_id: IntakeId) -> Intake | None: ...

    async def find_by_item(self, item_id: ItemId) -> list[Intake]: ...

    async def save(self, intake: Intake) -> Intake: ...


@runtime_checkable
class ArtifactRepository(Protocol):
    """Index cached artifacts without owning their payload bytes."""

    async def get(self, artifact_id: ArtifactId) -> ArtifactRecord | None: ...

    async def find_by_cache_key(self, cache_key: str) -> ArtifactRecord | None: ...

    async def save(self, artifact: ArtifactRecord) -> ArtifactRecord: ...


@runtime_checkable
class ClaimRepository(Protocol):
    """Append claims and record status changes without mutating claim values."""

    async def append(self, claim: Claim) -> Claim: ...

    async def list_by_item(self, item_id: ItemId) -> list[Claim]: ...

    async def list_by_field(self, item_id: ItemId, field: str) -> list[Claim]: ...

    async def append_status_event(
        self,
        claim_id: ClaimId,
        status: ClaimStatus | str,
        *,
        reason: str | None = None,
    ) -> ClaimEventRecord: ...


@runtime_checkable
class DecisionRepository(Protocol):
    """Append decision records and read their decisions."""

    async def save_record(self, record: DecisionRecord) -> DecisionRecord: ...

    async def get_record(self, record_id: DecisionRecordId) -> DecisionRecord | None: ...

    async def list_records(self, item_id: ItemId) -> list[DecisionRecord]: ...

    async def list_decisions(self, record_id: DecisionRecordId) -> list[Decision]: ...

    async def count_decisions(
        self,
        *,
        item_id: ItemId | None = None,
        unknown_only: bool = False,
    ) -> int: ...


@runtime_checkable
class LockRepository(Protocol):
    """Manage unique user locks per item and field."""

    async def get(self, item_id: ItemId, field: str = "") -> LockRecord | None: ...

    async def list_by_item(self, item_id: ItemId) -> list[LockRecord]: ...

    async def upsert(
        self,
        item_id: ItemId,
        field: str,
        value: FieldValue,
        *,
        source_claim_id: ClaimId | None = None,
    ) -> LockRecord: ...

    async def delete(self, item_id: ItemId, field: str = "") -> bool: ...


@runtime_checkable
class FeedbackRepository(Protocol):
    """Append feedback events and query them for review/statistics."""

    async def append_event(
        self,
        event: FeedbackEvent,
        *,
        applied: bool = False,
    ) -> FeedbackEvent: ...

    async def list_by_item(
        self,
        item_id: ItemId,
        *,
        kind: FeedbackKind | str | None = None,
        limit: int | None = None,
    ) -> list[FeedbackEvent]: ...

    async def list_by_field(self, item_id: ItemId, field: str) -> list[FeedbackEvent]: ...

    async def list_all(
        self,
        *,
        kind: FeedbackKind | str | None = None,
        item_id: ItemId | None = None,
        field: str | None = None,
        since: datetime | None = None,
        limit: int | None = None,
    ) -> list[FeedbackEvent]: ...

    async def stats(self, *, item_id: ItemId | None = None) -> FeedbackStats: ...

    async def save_session(self, session: ReviewSession) -> ReviewSession: ...

    async def get_session(self, session_id: ReviewSessionId) -> ReviewSession | None: ...

    async def list_sessions(
        self,
        *,
        item_id: ItemId | None = None,
        limit: int | None = None,
    ) -> list[ReviewSession]: ...

    async def update_session(
        self,
        session_id: ReviewSessionId,
        *,
        finished_at: datetime | None = None,
        notes: Sequence[str] | None = None,
    ) -> ReviewSession: ...


@runtime_checkable
class RunRepository(Protocol):
    """Persist pipeline runs, run items, and steps."""

    async def create_run(
        self,
        profile: str,
        mode: str,
        *,
        status: RunStatus | str = RunStatus.PLANNED,
        run_id: RunId | None = None,
        started_at: datetime | None = None,
        config_snapshot_hash: str | None = None,
    ) -> RunRecord: ...

    async def get_run(self, run_id: RunId) -> RunRecord | None: ...

    async def update_run(
        self,
        run_id: RunId,
        *,
        status: RunStatus | str | None = None,
        started_at: datetime | None = None,
        finished_at: datetime | None = None,
        error: dict[str, Any] | None = None,
        config_snapshot_hash: str | None = None,
    ) -> RunRecord: ...

    async def add_item(
        self,
        run_id: RunId,
        item_id: ItemId,
        *,
        status: RunItemStatus | str = RunItemStatus.PENDING,
    ) -> RunItemRecord: ...

    async def set_item_status(
        self,
        run_id: RunId,
        item_id: ItemId,
        status: RunItemStatus | str,
    ) -> RunItemRecord: ...

    async def create_step(
        self,
        run_id: RunId,
        item_id: ItemId,
        capability_id: str,
        capability_version: str,
        *,
        status: StepStatus | str = StepStatus.PENDING,
        step_id: StepId | None = None,
        cache_key: str | None = None,
    ) -> StepRecord: ...

    async def update_step(
        self,
        step_id: StepId,
        *,
        status: StepStatus | str | None = None,
        attempts: int | None = None,
        started_at: datetime | None = None,
        finished_at: datetime | None = None,
        error: dict[str, Any] | None = None,
        warnings: tuple[dict[str, Any] | str, ...] | None = None,
        cache_key: str | None = None,
    ) -> StepRecord: ...

    async def list_steps(
        self,
        run_id: RunId,
        *,
        item_id: ItemId | None = None,
    ) -> list[StepRecord]: ...

    async def list_steps_for_item(self, item_id: ItemId) -> list[StepRecord]: ...

    async def find_resumable(self) -> list[RunRecord]: ...


@runtime_checkable
class RenderRepository(Protocol):
    """Persist render operations and their output bundles."""

    async def save_render(self, render: Render) -> Render: ...

    async def save_bundle(self, bundle: OutputBundle) -> OutputBundle: ...

    async def get_bundle(self, bundle_id: str) -> OutputBundle | None: ...

    async def list_by_item(self, item_id: ItemId) -> list[Render]: ...


@runtime_checkable
class LibraryRepository(Protocol):
    """Track published files in the managed library."""

    async def save_entry(self, entry: LibraryEntry) -> LibraryEntry: ...

    async def find_by_path(self, path: str) -> LibraryEntry | None: ...

    async def list_entries(
        self,
        *,
        item_id: ItemId | None = None,
        status: LibraryStatus | str | None = None,
    ) -> list[LibraryEntry]: ...

    async def update_status(
        self,
        entry_id: LibraryEntryId,
        status: LibraryStatus | str,
    ) -> LibraryEntry: ...


@runtime_checkable
class UnitOfWork(Protocol):
    """Short-lived transaction boundary exposing catalog repositories."""

    items: ItemRepository
    media: MediaRepository
    file_instances: FileInstanceRepository
    intakes: IntakeRepository
    artifacts: ArtifactRepository
    claims: ClaimRepository
    decisions: DecisionRepository
    locks: LockRepository
    feedback: FeedbackRepository
    runs: RunRepository
    renders: RenderRepository
    library: LibraryRepository
    models: ModelRegistry

    async def __aenter__(self) -> UnitOfWork: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None: ...

    async def commit(self) -> None: ...

    async def rollback(self) -> None: ...


__all__ = [
    "ArtifactRepository",
    "ClaimRepository",
    "DecisionRepository",
    "FeedbackRepository",
    "FileInstanceRepository",
    "IntakeRepository",
    "ItemRepository",
    "LibraryRepository",
    "LockRepository",
    "MediaRepository",
    "ModelRegistry",
    "RenderRepository",
    "RunRepository",
    "UnitOfWork",
]
