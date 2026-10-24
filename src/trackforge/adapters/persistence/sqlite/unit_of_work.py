"""Unit of work for short catalog transactions.

Repositories share one SQLAlchemy session and one worker-thread executor.  A
unit of work is intentionally not safe for concurrent use by multiple tasks:
use one ``async with`` block per use case and keep heavy I/O outside it.
"""

from __future__ import annotations

import asyncio
from types import TracebackType

from sqlalchemy.orm import Session

from trackforge.adapters.persistence.sqlite._session import SessionRunner
from trackforge.adapters.persistence.sqlite.engine import Catalog
from trackforge.adapters.persistence.sqlite.repositories import (
    SqliteArtifactRepository,
    SqliteClaimRepository,
    SqliteDecisionRepository,
    SqliteFeedbackRepository,
    SqliteFileInstanceRepository,
    SqliteIntakeRepository,
    SqliteItemRepository,
    SqliteLibraryRepository,
    SqliteLockRepository,
    SqliteMediaRepository,
    SqliteModelRepository,
    SqliteRenderRepository,
    SqliteRunRepository,
)
from trackforge.application.ports.models import ModelRegistry
from trackforge.application.ports.repositories import (
    ArtifactRepository,
    ClaimRepository,
    DecisionRepository,
    FeedbackRepository,
    FileInstanceRepository,
    IntakeRepository,
    ItemRepository,
    LibraryRepository,
    LockRepository,
    MediaRepository,
    RenderRepository,
    RunRepository,
)


class SqliteUnitOfWork:
    """A short-lived catalog transaction with typed repository attributes."""

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

    def __init__(self, catalog: Catalog) -> None:
        self._catalog = catalog
        self._session: Session | None = None
        self._runner: SessionRunner | None = None

    @property
    def catalog(self) -> Catalog:
        return self._catalog

    @property
    def session(self) -> Session | None:
        return self._session

    async def __aenter__(self) -> SqliteUnitOfWork:
        session = await asyncio.to_thread(self._catalog.session_factory)
        runner = SessionRunner(session)
        self._session = session
        self._runner = runner
        self.items = SqliteItemRepository(runner)
        self.media = SqliteMediaRepository(runner)
        self.file_instances = SqliteFileInstanceRepository(runner)
        self.intakes = SqliteIntakeRepository(runner)
        self.artifacts = SqliteArtifactRepository(runner)
        self.claims = SqliteClaimRepository(runner)
        self.decisions = SqliteDecisionRepository(runner)
        self.locks = SqliteLockRepository(runner)
        self.feedback = SqliteFeedbackRepository(runner)
        self.runs = SqliteRunRepository(runner)
        self.renders = SqliteRenderRepository(runner)
        self.library = SqliteLibraryRepository(runner)
        self.models = SqliteModelRepository(runner)
        return self

    async def commit(self) -> None:
        """Commit the current transaction, if one is open."""

        session = self._session
        runner = self._runner
        if session is None or runner is None:
            return
        await runner.run(_commit)

    async def rollback(self) -> None:
        """Roll back the current transaction, if one is open."""

        session = self._session
        runner = self._runner
        if session is None or runner is None:
            return
        await runner.run(_rollback)

    async def close(self) -> None:
        """Close the session; safe to call more than once."""

        session = self._session
        self._session = None
        self._runner = None
        if session is None:
            return
        await asyncio.to_thread(session.close)

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        try:
            if exc_type is None:
                await self.commit()
            else:
                await self.rollback()
        finally:
            await self.close()


def _commit(session: Session) -> None:
    if session.in_transaction():
        session.commit()


def _rollback(session: Session) -> None:
    if session.in_transaction():
        session.rollback()


# Convenience alias matching the port name.  The concrete class keeps the
# adapter-specific prefix so it is unambiguous at composition roots.
UnitOfWork = SqliteUnitOfWork


__all__ = ["SqliteUnitOfWork", "UnitOfWork"]
