"""SQLite repository for managed-library entries."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from trackforge.adapters.persistence.sqlite._errors import translate_integrity_error
from trackforge.adapters.persistence.sqlite._mapping import (
    library_entry_from_row,
    to_iso_z,
)
from trackforge.adapters.persistence.sqlite.models import LibraryEntryRow
from trackforge.adapters.persistence.sqlite.repositories._base import SqliteRepository
from trackforge.domain.errors import RecordNotFound
from trackforge.domain.model._validation import coerce_enum
from trackforge.domain.model.ids import ItemId, LibraryEntryId
from trackforge.domain.model.outputs import LibraryEntry, LibraryStatus


class SqliteLibraryRepository(SqliteRepository):
    """Track files published into the user-visible library."""

    async def save_entry(self, entry: LibraryEntry) -> LibraryEntry:
        def _save(session: Session) -> LibraryEntry:
            row = LibraryEntryRow(
                id=entry.id,
                item_id=entry.item_id,
                bundle_id=entry.bundle_id,
                path=entry.path,
                status=entry.status.value,
                published_at=to_iso_z(entry.published_at),
            )
            session.add(row)
            session.flush()
            return library_entry_from_row(row)

        try:
            return await self._run(_save)
        except IntegrityError as exc:
            raise translate_integrity_error(exc) from exc

    async def find_by_path(self, path: str) -> LibraryEntry | None:
        def _find(session: Session) -> LibraryEntry | None:
            row = session.scalars(
                select(LibraryEntryRow)
                .where(LibraryEntryRow.path == path)
                .order_by(LibraryEntryRow.published_at.desc(), LibraryEntryRow.id)
                .limit(1)
            ).first()
            return None if row is None else library_entry_from_row(row)

        return await self._run(_find)

    async def list_entries(
        self,
        *,
        item_id: ItemId | None = None,
        status: LibraryStatus | str | None = None,
    ) -> list[LibraryEntry]:
        target = None if status is None else coerce_enum(status, LibraryStatus, "status")

        def _list(session: Session) -> list[LibraryEntry]:
            statement = select(LibraryEntryRow)
            if item_id is not None:
                statement = statement.where(LibraryEntryRow.item_id == item_id)
            if target is not None:
                statement = statement.where(LibraryEntryRow.status == target.value)
            statement = statement.order_by(
                LibraryEntryRow.published_at,
                LibraryEntryRow.id,
            )
            rows = session.scalars(statement).all()
            return [library_entry_from_row(row) for row in rows]

        return await self._run(_list)

    async def update_status(
        self,
        entry_id: LibraryEntryId,
        status: LibraryStatus | str,
    ) -> LibraryEntry:
        target = coerce_enum(status, LibraryStatus, "status")

        def _update(session: Session) -> LibraryEntry:
            row = session.get(LibraryEntryRow, entry_id)
            if row is None:
                raise RecordNotFound(
                    "library entry not found",
                    context={"entry_id": entry_id},
                )
            row.status = target.value
            session.flush()
            return library_entry_from_row(row)

        return await self._run(_update)


__all__ = ["SqliteLibraryRepository"]
