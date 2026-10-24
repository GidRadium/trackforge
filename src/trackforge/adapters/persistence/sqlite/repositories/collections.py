"""SQLite repository for collections and their ordered items."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from trackforge.adapters.persistence.sqlite._errors import translate_integrity_error
from trackforge.adapters.persistence.sqlite._mapping import (
    candidate_document,
    collection_context_document,
    collection_from_row,
    collection_item_from_row,
    source_ref_document,
    to_iso_z,
    to_json,
)
from trackforge.adapters.persistence.sqlite.models import CollectionItemRow, CollectionRow
from trackforge.adapters.persistence.sqlite.repositories._base import SqliteRepository
from trackforge.domain.errors import RecordNotFound
from trackforge.domain.model._validation import coerce_enum
from trackforge.domain.model.collection import Collection, CollectionItemStatus
from trackforge.domain.model.ids import CollectionId, ItemId
from trackforge.domain.model.source import SourceRef


class SqliteCollectionRepository(SqliteRepository):
    """Persist collection metadata while preserving per-item resume state."""

    async def get(self, collection_id: CollectionId) -> Collection | None:
        def _get(session: Session) -> Collection | None:
            row = session.get(CollectionRow, collection_id)
            return None if row is None else _load(session, row)

        return await self._run(_get)

    async def find_by_source(self, source_ref: SourceRef) -> Collection | None:
        source_json = to_json(source_ref_document(source_ref))

        def _find(session: Session) -> Collection | None:
            row = session.scalars(
                select(CollectionRow)
                .where(CollectionRow.source_ref_json == source_json)
                .order_by(CollectionRow.created_at.desc(), CollectionRow.id.desc())
                .limit(1)
            ).first()
            return None if row is None else _load(session, row)

        return await self._run(_find)

    async def list_all(self) -> list[Collection]:
        def _list(session: Session) -> list[Collection]:
            rows = session.scalars(
                select(CollectionRow).order_by(
                    CollectionRow.created_at.desc(),
                    CollectionRow.id.desc(),
                )
            ).all()
            return [_load(session, row) for row in rows]

        return await self._run(_list)

    async def save(self, collection: Collection) -> Collection:
        now = to_iso_z(datetime.now(UTC))

        def _save(session: Session) -> Collection:
            row = session.get(CollectionRow, collection.id)
            if row is None:
                row = CollectionRow(id=collection.id)
                session.add(row)
            row.kind = collection.kind.value
            row.source_ref_json = to_json(source_ref_document(collection.source_ref))
            row.title = collection.title
            row.owner = collection.owner
            row.description = collection.description
            row.context_claims_json = collection_context_document(collection)
            row.tag_basis = collection.tag_basis.value
            row.warnings_json = to_json(list(collection.warnings))
            row.created_at = to_iso_z(collection.created_at)

            for item in collection.items:
                item_row = session.get(
                    CollectionItemRow,
                    (collection.id, item.position),
                )
                if item_row is None:
                    item_row = CollectionItemRow(
                        collection_id=collection.id,
                        position=item.position,
                        candidate_json=to_json(candidate_document(item.candidate)),
                        item_id=item.item_id,
                        status=item.status.value,
                        error=item.error,
                        created_at=to_iso_z(item.updated_at or collection.created_at),
                        updated_at=to_iso_z(item.updated_at or collection.created_at),
                    )
                    session.add(item_row)
                else:
                    # Re-planning must never erase resume progress.
                    item_row.candidate_json = to_json(candidate_document(item.candidate))
                    item_row.updated_at = now
            session.flush()
            return _load(session, row)

        try:
            return await self._run(_save)
        except IntegrityError as exc:
            raise translate_integrity_error(exc) from exc

    async def update_item(
        self,
        collection_id: CollectionId,
        position: int,
        *,
        status: CollectionItemStatus | str,
        item_id: ItemId | None = None,
        error: str | None = None,
    ) -> Collection:
        target = coerce_enum(status, CollectionItemStatus, "status")
        now = to_iso_z(datetime.now(UTC))

        def _update(session: Session) -> Collection:
            row = session.get(CollectionRow, collection_id)
            if row is None:
                raise RecordNotFound(
                    "collection not found",
                    context={"collection_id": collection_id},
                )
            item_row = session.get(CollectionItemRow, (collection_id, position))
            if item_row is None:
                raise RecordNotFound(
                    "collection item not found",
                    context={"collection_id": collection_id, "position": position},
                )
            item_row.status = target.value
            item_row.item_id = item_id
            item_row.error = error
            item_row.updated_at = now
            session.flush()
            return _load(session, row)

        try:
            return await self._run(_update)
        except IntegrityError as exc:
            raise translate_integrity_error(exc) from exc


def _load(session: Session, row: CollectionRow) -> Collection:
    item_rows = session.scalars(
        select(CollectionItemRow)
        .where(CollectionItemRow.collection_id == row.id)
        .order_by(CollectionItemRow.position)
    ).all()
    return collection_from_row(row, [collection_item_from_row(item) for item in item_rows])


__all__ = ["SqliteCollectionRepository"]
