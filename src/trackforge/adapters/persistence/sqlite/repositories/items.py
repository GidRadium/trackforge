"""SQLite item repository."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from trackforge.adapters.persistence.sqlite._errors import translate_integrity_error
from trackforge.adapters.persistence.sqlite._mapping import item_from_row, to_iso_z
from trackforge.adapters.persistence.sqlite.models import ItemRow
from trackforge.adapters.persistence.sqlite.repositories._base import SqliteRepository
from trackforge.domain.errors import RecordNotFound
from trackforge.domain.model._validation import coerce_enum
from trackforge.domain.model.ids import ItemId, MediaId
from trackforge.domain.model.item import Item, ItemStatus


class SqliteItemRepository(SqliteRepository):
    """Persist item rows as domain aggregates."""

    async def get(self, item_id: ItemId) -> Item | None:
        def _get(session: Session) -> Item | None:
            row = session.get(ItemRow, item_id)
            return None if row is None else item_from_row(row)

        return await self._run(_get)

    async def save(self, item: Item) -> Item:
        def _save(session: Session) -> Item:
            row = session.get(ItemRow, item.id)
            if row is None:
                row = ItemRow(id=item.id)
                session.add(row)
            row.media_id = item.media_id
            row.primary_intake_id = item.primary_intake_id
            row.collection_id = item.collection_id
            row.kind = item.kind.value if item.kind is not None else None
            row.status = item.status.value
            row.current_decision_record_id = item.decision_record_id
            row.created_at = to_iso_z(item.created_at)
            row.updated_at = to_iso_z(item.updated_at or item.created_at)
            session.flush()
            return item_from_row(row)

        try:
            return await self._run(_save)
        except IntegrityError as exc:
            raise translate_integrity_error(exc) from exc

    async def update_status(
        self,
        item_id: ItemId,
        status: ItemStatus | str,
        *,
        at: datetime | None = None,
    ) -> Item:
        target = coerce_enum(status, ItemStatus, "status")

        def _update(session: Session) -> Item:
            row = session.get(ItemRow, item_id)
            if row is None:
                raise RecordNotFound(
                    "item not found",
                    context={"item_id": item_id},
                )
            row.status = target.value
            row.updated_at = to_iso_z(at if at is not None else datetime.now(UTC))
            session.flush()
            return item_from_row(row)

        return await self._run(_update)

    async def find_by_media(self, media_id: MediaId) -> list[Item]:
        def _find(session: Session) -> list[Item]:
            rows = session.scalars(
                select(ItemRow)
                .where(ItemRow.media_id == media_id)
                .order_by(ItemRow.created_at, ItemRow.id)
            ).all()
            return [item_from_row(row) for row in rows]

        return await self._run(_find)

    async def list_by_status(
        self,
        status: ItemStatus | str,
        *,
        limit: int | None = None,
    ) -> list[Item]:
        target = coerce_enum(status, ItemStatus, "status")

        def _list(session: Session) -> list[Item]:
            statement = (
                select(ItemRow)
                .where(ItemRow.status == target.value)
                .order_by(ItemRow.created_at, ItemRow.id)
            )
            if limit is not None:
                statement = statement.limit(limit)
            rows = session.scalars(statement).all()
            return [item_from_row(row) for row in rows]

        return await self._run(_list)


__all__ = ["SqliteItemRepository"]
