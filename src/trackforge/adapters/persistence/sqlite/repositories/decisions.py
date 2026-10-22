"""SQLite append-only decision-record repository."""

from __future__ import annotations

from dataclasses import replace

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from trackforge.adapters.persistence.sqlite._errors import translate_integrity_error
from trackforge.adapters.persistence.sqlite._mapping import (
    decision_from_row,
    decision_record_from_row,
    to_iso_z,
    to_json,
)
from trackforge.adapters.persistence.sqlite.models import DecisionRecordRow, DecisionRow
from trackforge.adapters.persistence.sqlite.repositories._base import SqliteRepository
from trackforge.domain.model.claims import Subject
from trackforge.domain.model.decisions import Decision, DecisionRecord
from trackforge.domain.model.ids import DecisionId, DecisionRecordId, ItemId, new_uuid7


class SqliteDecisionRepository(SqliteRepository):
    """Persist new decision records; existing records are never updated."""

    async def save_record(self, record: DecisionRecord) -> DecisionRecord:
        materialized = _materialize_decisions(record)

        def _save(session: Session) -> DecisionRecord:
            session.add(
                DecisionRecordRow(
                    id=materialized.id,
                    item_id=materialized.item_id,
                    version=materialized.version,
                    policy_versions_json=to_json(list(materialized.policy_versions)),
                    llm_model=materialized.llm_model,
                    prompt_version=materialized.prompt_version,
                    input_claim_ids_json=to_json(
                        [str(item) for item in materialized.input_claim_ids]
                    ),
                    created_at=to_iso_z(materialized.created_at),
                )
            )
            session.flush()
            for decision in materialized.decisions:
                decision_id = decision.id if decision.id is not None else DecisionId(new_uuid7())
                session.add(
                    DecisionRow(
                        id=decision_id,
                        record_id=materialized.id,
                        subject_type=decision.subject.value,
                        subject_id=(
                            materialized.item_id if decision.subject is Subject.ITEM else None
                        ),
                        field=decision.field,
                        value_json=to_json(decision.value),
                        chosen_claim_id=decision.chosen_claim_id,
                        alternatives_json=to_json([str(item) for item in decision.alternatives]),
                        derived_from_json=to_json([str(item) for item in decision.derived_from]),
                        policy_id=decision.policy_id,
                        mode=decision.mode.value,
                        decided_by=decision.decided_by.value,
                        confidence=decision.confidence,
                        rationale=decision.rationale,
                        locked=decision.locked,
                        created_at=to_iso_z(decision.created_at),
                    )
                )
            session.flush()
            return materialized

        try:
            return await self._run(_save)
        except IntegrityError as exc:
            raise translate_integrity_error(exc) from exc

    async def get_record(self, record_id: DecisionRecordId) -> DecisionRecord | None:
        def _get(session: Session) -> DecisionRecord | None:
            row = session.get(DecisionRecordRow, record_id)
            if row is None:
                return None
            decisions = _load_decisions(session, record_id)
            return decision_record_from_row(row, decisions)

        return await self._run(_get)

    async def list_records(self, item_id: ItemId) -> list[DecisionRecord]:
        def _list(session: Session) -> list[DecisionRecord]:
            rows = session.scalars(
                select(DecisionRecordRow)
                .where(DecisionRecordRow.item_id == item_id)
                .order_by(DecisionRecordRow.version, DecisionRecordRow.created_at)
            ).all()
            return [
                decision_record_from_row(row, _load_decisions(session, DecisionRecordId(row.id)))
                for row in rows
            ]

        return await self._run(_list)

    async def list_decisions(self, record_id: DecisionRecordId) -> list[Decision]:
        def _list(session: Session) -> list[Decision]:
            return list(_load_decisions(session, record_id))

        return await self._run(_list)


def _materialize_decisions(record: DecisionRecord) -> DecisionRecord:
    decisions: list[Decision] = []
    changed = False
    for decision in record.decisions:
        if decision.id is None:
            decisions.append(replace(decision, id=DecisionId(new_uuid7())))
            changed = True
        else:
            decisions.append(decision)
    if not changed:
        return record
    return replace(record, decisions=tuple(decisions))


def _load_decisions(
    session: Session,
    record_id: DecisionRecordId,
) -> tuple[Decision, ...]:
    rows = session.scalars(
        select(DecisionRow)
        .where(DecisionRow.record_id == record_id)
        .order_by(DecisionRow.created_at, DecisionRow.id)
    ).all()
    return tuple(decision_from_row(row) for row in rows)


__all__ = ["SqliteDecisionRepository"]
