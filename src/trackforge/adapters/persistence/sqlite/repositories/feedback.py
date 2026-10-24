"""SQLite repository for append-only feedback events and review sessions."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from trackforge.adapters.persistence.sqlite._errors import translate_integrity_error
from trackforge.adapters.persistence.sqlite._mapping import (
    feedback_from_row,
    from_iso_z,
    to_iso_z,
    to_json,
)
from trackforge.adapters.persistence.sqlite.models import (
    FeedbackEventRow,
    ReviewSessionItemRow,
    ReviewSessionRow,
)
from trackforge.adapters.persistence.sqlite.repositories._base import SqliteRepository
from trackforge.application.dto.db import FeedbackStats
from trackforge.domain.errors import InvalidCatalogData, RecordNotFound
from trackforge.domain.model._validation import coerce_enum
from trackforge.domain.model.feedback import (
    FeedbackEvent,
    FeedbackKind,
    ReviewSession,
)
from trackforge.domain.model.ids import (
    CorrectionId,
    ItemId,
    ReviewSessionId,
)


class SqliteFeedbackRepository(SqliteRepository):
    """Append feedback rows and expose query/statistics/session helpers."""

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
                reason_code=(event.reason_code.value if event.reason_code is not None else None),
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
        return await self.list_all(item_id=item_id, kind=kind, limit=limit)

    async def list_by_field(self, item_id: ItemId, field: str) -> list[FeedbackEvent]:
        return await self.list_all(item_id=item_id, field=field)

    async def list_all(
        self,
        *,
        kind: FeedbackKind | str | None = None,
        item_id: ItemId | None = None,
        field: str | None = None,
        since: datetime | None = None,
        limit: int | None = None,
    ) -> list[FeedbackEvent]:
        target = None if kind is None else coerce_enum(kind, FeedbackKind, "kind")

        def _list(session: Session) -> list[FeedbackEvent]:
            statement = select(FeedbackEventRow)
            if item_id is not None:
                statement = statement.where(FeedbackEventRow.item_id == item_id)
            if target is not None:
                statement = statement.where(FeedbackEventRow.kind == target.value)
            if field is not None:
                statement = statement.where(FeedbackEventRow.field == field)
            if since is not None:
                statement = statement.where(FeedbackEventRow.created_at >= to_iso_z(since))
            statement = statement.order_by(
                FeedbackEventRow.created_at,
                FeedbackEventRow.id,
            )
            if limit is not None:
                statement = statement.limit(limit)
            rows = session.scalars(statement).all()
            return [feedback_from_row(row) for row in rows]

        return await self._run(_list)

    async def stats(self, *, item_id: ItemId | None = None) -> FeedbackStats:
        def _stats(session: Session) -> FeedbackStats:
            statement = select(FeedbackEventRow)
            if item_id is not None:
                statement = statement.where(FeedbackEventRow.item_id == item_id)
            rows = list(session.scalars(statement).all())

            by_kind: dict[str, int] = {}
            by_field: dict[str, int] = {}
            by_source: dict[str, int] = {}
            by_method: dict[str, int] = {}
            counters = {
                "correction": 0,
                "approval": 0,
                "rejection": 0,
                "lock": 0,
                "unlock": 0,
                "review_note": 0,
                "unknowns": 0,
            }
            for row in rows:
                by_kind[row.kind] = by_kind.get(row.kind, 0) + 1
                if row.field is not None:
                    by_field[row.field] = by_field.get(row.field, 0) + 1
                context = _context_mapping(row.context_snapshot_json)
                source = _snapshot_dimension(context, "source")
                method = _snapshot_dimension(context, "method")
                by_source[source] = by_source.get(source, 0) + 1
                by_method[method] = by_method.get(method, 0) + 1
                if row.kind in counters:
                    counters[row.kind] += 1
                if row.kind == FeedbackKind.CORRECTION.value and row.user_value_json is None:
                    counters["unknowns"] += 1

            return FeedbackStats(
                total=len(rows),
                by_kind=by_kind,
                by_field=by_field,
                by_source=by_source,
                by_method=by_method,
                corrections=counters["correction"],
                approvals=counters["approval"],
                rejections=counters["rejection"],
                locks=counters["lock"],
                unlocks=counters["unlock"],
                review_notes=counters["review_note"],
                unknowns=counters["unknowns"],
            )

        return await self._run(_stats)

    # ------------------------------------------------------------------
    # Review sessions
    # ------------------------------------------------------------------
    async def save_session(self, session: ReviewSession) -> ReviewSession:
        def _save(db: Session) -> ReviewSession:
            db.add(
                ReviewSessionRow(
                    id=session.id,
                    started_at=to_iso_z(session.started_at),
                    finished_at=(
                        to_iso_z(session.finished_at) if session.finished_at is not None else None
                    ),
                    fields_json=to_json(list(session.fields)),
                    notes=to_json(list(session.notes)),
                )
            )
            db.flush()
            for item_id in session.item_ids:
                db.add(ReviewSessionItemRow(session_id=session.id, item_id=item_id))
            db.flush()
            return self._session_from_db(db, session.id) or session

        try:
            return await self._run(_save)
        except IntegrityError as exc:
            raise translate_integrity_error(exc) from exc

    async def get_session(self, session_id: ReviewSessionId) -> ReviewSession | None:
        def _get(db: Session) -> ReviewSession | None:
            return self._session_from_db(db, session_id)

        return await self._run(_get)

    async def list_sessions(
        self,
        *,
        item_id: ItemId | None = None,
        limit: int | None = None,
    ) -> list[ReviewSession]:
        def _list(db: Session) -> list[ReviewSession]:
            statement = select(ReviewSessionRow)
            if item_id is not None:
                statement = statement.join(
                    ReviewSessionItemRow,
                    ReviewSessionItemRow.session_id == ReviewSessionRow.id,
                ).where(ReviewSessionItemRow.item_id == item_id)
            statement = statement.order_by(
                ReviewSessionRow.started_at,
                ReviewSessionRow.id,
            )
            if limit is not None:
                statement = statement.limit(limit)
            return [
                session
                for row in db.scalars(statement).all()
                if (session := self._session_from_db(db, ReviewSessionId(row.id))) is not None
            ]

        return await self._run(_list)

    async def update_session(
        self,
        session_id: ReviewSessionId,
        *,
        finished_at: datetime | None = None,
        notes: Sequence[str] | None = None,
    ) -> ReviewSession:
        def _update(db: Session) -> ReviewSession:
            row = db.get(ReviewSessionRow, session_id)
            if row is None:
                raise RecordNotFound(
                    "review session not found",
                    context={"session_id": str(session_id)},
                )
            if finished_at is not None:
                row.finished_at = to_iso_z(finished_at)
            if notes is not None:
                row.notes = to_json([str(note) for note in notes])
            db.flush()
            session = self._session_from_db(db, session_id)
            if session is None:  # pragma: no cover - row exists above
                raise RecordNotFound(
                    "review session not found",
                    context={"session_id": str(session_id)},
                )
            return session

        try:
            return await self._run(_update)
        except IntegrityError as exc:  # pragma: no cover - defensive
            raise translate_integrity_error(exc) from exc

    def _session_from_db(
        self,
        db: Session,
        session_id: ReviewSessionId,
    ) -> ReviewSession | None:
        row = db.get(ReviewSessionRow, session_id)
        if row is None:
            return None
        items = db.scalars(
            select(ReviewSessionItemRow)
            .where(ReviewSessionItemRow.session_id == session_id)
            .order_by(ReviewSessionItemRow.item_id)
        ).all()
        correction_rows = db.scalars(
            select(FeedbackEventRow.id)
            .where(
                FeedbackEventRow.review_session_id == session_id,
                FeedbackEventRow.kind == FeedbackKind.CORRECTION.value,
            )
            .order_by(FeedbackEventRow.created_at, FeedbackEventRow.id)
        ).all()
        return ReviewSession(
            id=ReviewSessionId(row.id),
            item_ids=tuple(ItemId(item.item_id) for item in items),
            fields=tuple(_string_list(row.fields_json, "fields")),
            started_at=from_iso_z(row.started_at),
            finished_at=from_iso_z(row.finished_at) if row.finished_at else None,
            corrections=tuple(CorrectionId(str(value)) for value in correction_rows),
            notes=tuple(_string_list(row.notes, "notes")),
        )


def _string_list(value: str | None, field: str) -> list[str]:
    if value is None:
        return []
    try:
        decoded = json.loads(value)
    except (TypeError, json.JSONDecodeError) as exc:
        raise InvalidCatalogData(
            f"review session {field} is corrupt",
            context={"field": field},
        ) from exc
    if not isinstance(decoded, list) or not all(isinstance(item, str) for item in decoded):
        raise InvalidCatalogData(
            f"review session {field} must be a JSON list of strings",
            context={"field": field},
        )
    return [str(item) for item in decoded]


def _context_mapping(value: str | None) -> Mapping[str, object]:
    if not value:
        return {}
    try:
        decoded = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return {}
    return decoded if isinstance(decoded, Mapping) else {}


def _snapshot_dimension(context: Mapping[str, object], dimension: str) -> str:
    direct = context.get(f"system_{dimension}") or context.get(f"decision_{dimension}")
    if isinstance(direct, str) and direct.strip():
        return direct.strip()
    claims = context.get("claims")
    chosen_id = context.get("chosen_claim_id")
    if isinstance(claims, list):
        for claim in claims:
            if not isinstance(claim, Mapping):
                continue
            if chosen_id is not None and str(claim.get("id")) != str(chosen_id):
                continue
            value = claim.get(dimension)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return "unknown"


__all__ = ["SqliteFeedbackRepository"]
