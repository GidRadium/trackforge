"""SQLite repository for item/field locks."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from trackforge.adapters.persistence.sqlite._errors import translate_integrity_error
from trackforge.adapters.persistence.sqlite._mapping import lock_from_row, to_iso_z, to_json
from trackforge.adapters.persistence.sqlite.models import LockRow
from trackforge.adapters.persistence.sqlite.repositories._base import SqliteRepository
from trackforge.application.dto.db import LockRecord
from trackforge.domain.model._validation import FieldValue, validate_field_value
from trackforge.domain.model.ids import ClaimId, ItemId, new_uuid7


class SqliteLockRepository(SqliteRepository):
    """Manage the unique ``(item_id, field)`` lock rows."""

    async def get(self, item_id: ItemId, field: str = "") -> LockRecord | None:
        def _get(session: Session) -> LockRecord | None:
            row = session.scalar(
                select(LockRow).where(LockRow.item_id == item_id, LockRow.field == field)
            )
            return None if row is None else lock_from_row(row)

        return await self._run(_get)

    async def list_by_item(self, item_id: ItemId) -> list[LockRecord]:
        def _list(session: Session) -> list[LockRecord]:
            rows = session.scalars(
                select(LockRow).where(LockRow.item_id == item_id).order_by(LockRow.field)
            ).all()
            return [lock_from_row(row) for row in rows]

        return await self._run(_list)

    async def upsert(
        self,
        item_id: ItemId,
        field: str,
        value: FieldValue,
        *,
        source_claim_id: ClaimId | None = None,
    ) -> LockRecord:
        if not isinstance(field, str):
            raise ValueError("field must be a string")
        validated = validate_field_value(value, "value")
        now = datetime.now(UTC)

        def _upsert(session: Session) -> LockRecord:
            row = session.scalar(
                select(LockRow).where(LockRow.item_id == item_id, LockRow.field == field)
            )
            if row is None:
                row = LockRow(
                    id=new_uuid7(),
                    item_id=item_id,
                    field=field,
                    value_json=to_json(validated),
                    source_claim_id=source_claim_id,
                    created_at=to_iso_z(now),
                    updated_at=to_iso_z(now),
                )
                session.add(row)
            else:
                row.value_json = to_json(validated)
                row.source_claim_id = source_claim_id
                row.updated_at = to_iso_z(now)
            session.flush()
            return lock_from_row(row)

        try:
            return await self._run(_upsert)
        except IntegrityError as exc:
            raise translate_integrity_error(exc) from exc

    async def delete(self, item_id: ItemId, field: str = "") -> bool:
        def _delete(session: Session) -> bool:
            result = session.execute(
                delete(LockRow).where(LockRow.item_id == item_id, LockRow.field == field)
            )
            session.flush()
            return bool(getattr(result, "rowcount", 0))

        return await self._run(_delete)


__all__ = ["SqliteLockRepository"]
