"""SQLite repository for concrete media file instances."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from trackforge.adapters.persistence.sqlite._errors import translate_integrity_error
from trackforge.adapters.persistence.sqlite._mapping import (
    file_instance_from_row,
    to_iso_z,
    to_json,
)
from trackforge.adapters.persistence.sqlite.models import FileInstanceRow
from trackforge.adapters.persistence.sqlite.repositories._base import SqliteRepository
from trackforge.domain.model.ids import FileInstanceId, MediaId
from trackforge.domain.model.media import FileInstance


class SqliteFileInstanceRepository(SqliteRepository):
    """Persist source/CAS/render/library appearances of media bytes."""

    async def get(self, file_instance_id: FileInstanceId) -> FileInstance | None:
        def _get(session: Session) -> FileInstance | None:
            row = session.get(FileInstanceRow, file_instance_id)
            return None if row is None else file_instance_from_row(row)

        return await self._run(_get)

    async def find_by_media(self, media_id: MediaId) -> list[FileInstance]:
        def _find(session: Session) -> list[FileInstance]:
            rows = session.scalars(
                select(FileInstanceRow)
                .where(FileInstanceRow.media_id == media_id)
                .order_by(FileInstanceRow.created_at, FileInstanceRow.id)
            ).all()
            return [file_instance_from_row(row) for row in rows]

        return await self._run(_find)

    async def find_by_path(self, path: str) -> list[FileInstance]:
        def _find(session: Session) -> list[FileInstance]:
            rows = session.scalars(
                select(FileInstanceRow)
                .where(FileInstanceRow.path == path)
                .order_by(FileInstanceRow.created_at, FileInstanceRow.id)
            ).all()
            return [file_instance_from_row(row) for row in rows]

        return await self._run(_find)

    async def save(self, instance: FileInstance) -> FileInstance:
        def _save(session: Session) -> FileInstance:
            raw_mtime: str | float
            if isinstance(instance.mtime, datetime):
                raw_mtime = to_iso_z(instance.mtime)
            else:
                raw_mtime = float(instance.mtime)
            row = FileInstanceRow(
                id=instance.id,
                media_id=instance.media_id,
                path=instance.path,
                role=instance.role.value,
                size_bytes=instance.size_bytes,
                mtime=to_json(raw_mtime),
                intake_id=instance.intake_id,
                created_at=to_iso_z(instance.created_at),
            )
            session.add(row)
            session.flush()
            return file_instance_from_row(row)

        try:
            return await self._run(_save)
        except IntegrityError as exc:
            raise translate_integrity_error(exc) from exc


__all__ = ["SqliteFileInstanceRepository"]
