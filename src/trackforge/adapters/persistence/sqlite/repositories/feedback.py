"""SQLite repository for append-only feedback events."""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from trackforge.adapters.persistence.sqlite._errors import translate_integrity_error
from trackforge.adapters.persistence.sqlite._mapping import feedback_from_row, to_iso_z, to_json
from trackforge.adapters.persistence.sqlite.models import FeedbackEventRow
from trackforge.adapters.persistence.sqlite.repositories._base import SqliteRepository
from trackforge.application.dto.db import FeedbackStats
from trackforge.domain.model._validation import coerce_enum
from trackforge.domain.model.feedback import FeedbackEvent, FeedbackKind
from trackforge.domain.model.ids import ItemId


class SqliteFeedbackRepository(SqliteRepository):
    """Append feedback rows and expose query/statistics helpers."""

    async def append_event(
        self,
        event: FeedbackEvent,
        *,
        applied: bool = False,
    ) -> FeedbackEvent:
        def _append(session: Session) -> FeedbackEvent:
            row = FeedbackEventRow(
                id=event.id,
                kind=event.kind.value,
                item_id=event.item_id,
                field=event.field,
                system_decision_id=event.system_decision_id,
                system_value_json=(
                    to_json(event.system_value) if event.system_value is not None else None
                ),
                user_claim_id=event.user_claim_id,
                user_value_json=(
                    to_json(event.user_value) if event.user_value is not None else None
                ),
                scope=event.scope.value if event.scope is not None else None,
                reason_code=None,
                reason_text=event.reason,
                review_session_id=event.review_session_id,
                context_snapshot_json=(
                    to_json(event.context_snapshot) if event.context_snapshot is not None else None
                ),
                created_at=to_iso_z(event.created_at),
                applied=applied,
            )
            session.add(row)
            session.flush()
            return feedback_from_row(row)

        try:
            return await self._run(_append)
        except IntegrityError as exc:
            raise translate_integrity_error(exc) from exc

    async def list_by_item(
        self,
        item_id: ItemId,
        *,
        kind: FeedbackKind | str | None = None,
        limit: int | None = None,
    ) -> list[FeedbackEvent]:
        target = None if kind is None else coerce_enum(kind, FeedbackKind, "kind")

        def _list(session: Session) -> list[FeedbackEvent]:
            statement = select(FeedbackEventRow).where(FeedbackEventRow.item_id == item_id)
            if target is not None:
                statement = statement.where(FeedbackEventRow.kind == target.value)
            statement = statement.order_by(
                FeedbackEventRow.created_at,
                FeedbackEventRow.id,
            )
            if limit is not None:
                statement = statement.limit(limit)
            rows = session.scalars(statement).all()
            return [feedback_from_row(row) for row in rows]

        return await self._run(_list)

    async def list_by_field(self, item_id: ItemId, field: str) -> list[FeedbackEvent]:
        def _list(session: Session) -> list[FeedbackEvent]:
            rows = session.scalars(
                select(FeedbackEventRow)
                .where(
                    FeedbackEventRow.item_id == item_id,
                    FeedbackEventRow.field == field,
                )
                .order_by(FeedbackEventRow.created_at, FeedbackEventRow.id)
            ).all()
            return [feedback_from_row(row) for row in rows]

        return await self._run(_list)

    async def stats(self, *, item_id: ItemId | None = None) -> FeedbackStats:
        def _stats(session: Session) -> FeedbackStats:
            total_statement = select(func.count()).select_from(FeedbackEventRow)
            if item_id is not None:
                total_statement = total_statement.where(FeedbackEventRow.item_id == item_id)
            total = int(session.scalar(total_statement) or 0)

            kind_statement = select(
                FeedbackEventRow.kind,
                func.count(),
            ).group_by(FeedbackEventRow.kind)
            field_statement = (
                select(
                    FeedbackEventRow.field,
                    func.count(),
                )
                .where(FeedbackEventRow.field.is_not(None))
                .group_by(FeedbackEventRow.field)
            )
            if item_id is not None:
                kind_statement = kind_statement.where(FeedbackEventRow.item_id == item_id)
                field_statement = field_statement.where(FeedbackEventRow.item_id == item_id)

            by_kind = {str(kind): int(count) for kind, count in session.execute(kind_statement)}
            by_field = {
                str(field): int(count)
                for field, count in session.execute(field_statement)
                if field is not None
            }
            return FeedbackStats(total=total, by_kind=by_kind, by_field=by_field)

        return await self._run(_stats)


__all__ = ["SqliteFeedbackRepository"]
