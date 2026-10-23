"""SQLite intake repository."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from trackforge.adapters.persistence.sqlite._errors import translate_integrity_error
from trackforge.adapters.persistence.sqlite._mapping import (
    candidate_document,
    intake_from_row,
    source_ref_document,
    to_iso_z,
    to_json,
)
from trackforge.adapters.persistence.sqlite.models import IntakeRow
from trackforge.adapters.persistence.sqlite.repositories._base import SqliteRepository
from trackforge.domain.model.ids import IntakeId, ItemId
from trackforge.domain.model.media import Intake
from trackforge.domain.model.source import SourceRef


class SqliteIntakeRepository(SqliteRepository):
    """Persist acquisition facts and their source snapshots."""

    async def get(self, intake_id: IntakeId) -> Intake | None:
        def _get(session: Session) -> Intake | None:
            row = session.get(IntakeRow, intake_id)
            return None if row is None else intake_from_row(row)

        return await self._run(_get)

    async def find_by_item(self, item_id: ItemId) -> list[Intake]:
        def _find(session: Session) -> list[Intake]:
            rows = session.scalars(
                select(IntakeRow)
                .where(IntakeRow.item_id == item_id)
                .order_by(IntakeRow.created_at, IntakeRow.id)
            ).all()
            return [intake_from_row(row) for row in rows]

        return await self._run(_find)

    async def find_by_source_ref(self, source_ref: SourceRef) -> list[Intake]:
        """Return intakes for one exact source reference, oldest first."""

        document = to_json(source_ref_document(source_ref))

        def _find(session: Session) -> list[Intake]:
            rows = session.scalars(
                select(IntakeRow)
                .where(IntakeRow.source_ref_json == document)
                .order_by(IntakeRow.created_at, IntakeRow.id)
            ).all()
            return [intake_from_row(row) for row in rows]

        return await self._run(_find)

    async def save(self, intake: Intake) -> Intake:
        def _save(session: Session) -> Intake:
            row = IntakeRow(
                id=intake.id,
                item_id=intake.item_id,
                source_ref_json=to_json(source_ref_document(intake.source_ref)),
                candidate_json=to_json(candidate_document(intake.candidate)),
                source_snapshot_artifact_id=intake.source_snapshot_artifact_id,
                downloader_id=intake.downloader,
                downloader_version=intake.downloader_version,
                media_id=intake.media_id,
                status=intake.status.value,
                created_at=to_iso_z(intake.created_at),
            )
            session.add(row)
            session.flush()
            return intake_from_row(row)

        try:
            return await self._run(_save)
        except IntegrityError as exc:
            raise translate_integrity_error(exc) from exc


__all__ = ["SqliteIntakeRepository"]
