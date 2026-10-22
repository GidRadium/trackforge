"""SQLite claim repository with append-only claim and event writes."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from trackforge.adapters.persistence.sqlite._errors import translate_integrity_error
from trackforge.adapters.persistence.sqlite._mapping import (
    claim_from_row,
    split_method,
    to_iso_z,
    to_json,
)
from trackforge.adapters.persistence.sqlite.models import ClaimEventRow, ClaimRow
from trackforge.adapters.persistence.sqlite.repositories._base import SqliteRepository
from trackforge.application.dto.db import ClaimEventRecord
from trackforge.domain.errors import RecordNotFound
from trackforge.domain.model._validation import coerce_enum
from trackforge.domain.model.claims import Claim, ClaimStatus
from trackforge.domain.model.ids import ClaimId, ItemId, new_uuid7


class SqliteClaimRepository(SqliteRepository):
    """Append claims and status events without exposing generic updates."""

    async def append(self, claim: Claim) -> Claim:
        method_id, method_version = split_method(claim.method)
        notes = claim.notes
        if notes is None and not claim.evidence and claim.evidence_reason is not None:
            # The schema has no dedicated evidence_reason column; retain the
            # explanation rather than dropping the only evidence note.
            notes = claim.evidence_reason

        def _append(session: Session) -> Claim:
            row = ClaimRow(
                id=claim.id,
                item_id=claim.item_id,
                subject_type=claim.subject.value,
                subject_id=claim.subject_id,
                field=claim.field,
                value_json=to_json(claim.value),
                role=claim.role,
                order_index=claim.order,
                locale=claim.locale,
                source=claim.source,
                method_id=method_id,
                method_version=method_version,
                confidence=float(claim.confidence),
                evidence_json=to_json(list(claim.evidence)),
                derived_from_json=to_json(list(claim.derived_from)),
                status=claim.status.value,
                notes=notes,
                created_at=to_iso_z(claim.created_at),
            )
            session.add(row)
            session.flush()
            return claim_from_row(row)

        try:
            return await self._run(_append)
        except IntegrityError as exc:
            raise translate_integrity_error(exc) from exc

    async def list_by_item(self, item_id: ItemId) -> list[Claim]:
        def _list(session: Session) -> list[Claim]:
            rows = session.scalars(
                select(ClaimRow)
                .where(ClaimRow.item_id == item_id)
                .order_by(ClaimRow.created_at, ClaimRow.id)
            ).all()
            return [claim_from_row(row) for row in rows]

        return await self._run(_list)

    async def list_by_field(self, item_id: ItemId, field: str) -> list[Claim]:
        def _list(session: Session) -> list[Claim]:
            rows = session.scalars(
                select(ClaimRow)
                .where(ClaimRow.item_id == item_id, ClaimRow.field == field)
                .order_by(ClaimRow.created_at, ClaimRow.id)
            ).all()
            return [claim_from_row(row) for row in rows]

        return await self._run(_list)

    async def append_status_event(
        self,
        claim_id: ClaimId,
        status: ClaimStatus | str,
        *,
        reason: str | None = None,
    ) -> ClaimEventRecord:
        target = coerce_enum(status, ClaimStatus, "status")
        now = datetime.now(UTC)

        def _append(session: Session) -> ClaimEventRecord:
            claim = session.get(ClaimRow, claim_id)
            if claim is None:
                raise RecordNotFound(
                    "claim not found",
                    context={"claim_id": claim_id},
                )
            event = ClaimEventRow(
                id=new_uuid7(),
                claim_id=claim_id,
                status=target.value,
                reason=reason,
                created_at=to_iso_z(now),
            )
            session.add(event)
            # ``claims.status`` is a projection; the event is the history.
            claim.status = target.value
            session.flush()
            return ClaimEventRecord(
                id=event.id,
                claim_id=ClaimId(event.claim_id),
                status=target,
                reason=event.reason,
                created_at=now,
            )

        try:
            return await self._run(_append)
        except IntegrityError as exc:
            raise translate_integrity_error(exc) from exc


__all__ = ["SqliteClaimRepository"]
