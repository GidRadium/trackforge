"""SQLite repository for pipeline runs, run items, and steps."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from trackforge.adapters.persistence.sqlite._errors import translate_integrity_error
from trackforge.adapters.persistence.sqlite._mapping import (
    optional_iso_z,
    run_from_row,
    run_item_from_row,
    step_from_row,
    to_iso_z,
    to_json,
)
from trackforge.adapters.persistence.sqlite.models import (
    RunItemRow,
    RunRow,
    StepRow,
)
from trackforge.adapters.persistence.sqlite.repositories._base import SqliteRepository
from trackforge.application.dto.db import (
    RunItemRecord,
    RunItemStatus,
    RunRecord,
    RunStatus,
    StepRecord,
    StepStatus,
)
from trackforge.domain.errors import RecordNotFound
from trackforge.domain.model._validation import coerce_enum
from trackforge.domain.model.ids import ItemId, RunId, StepId, new_uuid7


class SqliteRunRepository(SqliteRepository):
    """Persist run/step state without owning executor policy."""

    async def create_run(
        self,
        profile: str,
        mode: str,
        *,
        status: RunStatus | str = RunStatus.PLANNED,
        run_id: RunId | None = None,
        started_at: datetime | None = None,
        config_snapshot_hash: str | None = None,
    ) -> RunRecord:
        target = coerce_enum(status, RunStatus, "status")
        identifier = RunId(run_id) if run_id is not None else RunId(new_uuid7())

        def _create(session: Session) -> RunRecord:
            row = RunRow(
                id=identifier,
                profile=profile,
                mode=mode,
                status=target.value,
                started_at=optional_iso_z(started_at),
                config_snapshot_hash=config_snapshot_hash,
            )
            session.add(row)
            session.flush()
            return run_from_row(row)

        try:
            return await self._run(_create)
        except IntegrityError as exc:
            raise translate_integrity_error(exc) from exc

    async def get_run(self, run_id: RunId) -> RunRecord | None:
        def _get(session: Session) -> RunRecord | None:
            row = session.get(RunRow, run_id)
            return None if row is None else run_from_row(row)

        return await self._run(_get)

    async def update_run(
        self,
        run_id: RunId,
        *,
        status: RunStatus | str | None = None,
        started_at: datetime | None = None,
        finished_at: datetime | None = None,
        error: dict[str, Any] | None = None,
        config_snapshot_hash: str | None = None,
    ) -> RunRecord:
        target = None if status is None else coerce_enum(status, RunStatus, "status")

        def _update(session: Session) -> RunRecord:
            row = session.get(RunRow, run_id)
            if row is None:
                raise RecordNotFound(
                    "run not found",
                    context={"run_id": run_id},
                )
            if target is not None:
                row.status = target.value
            if started_at is not None:
                row.started_at = to_iso_z(started_at)
            if finished_at is not None:
                row.finished_at = to_iso_z(finished_at)
            if error is not None:
                row.error_json = to_json(error)
            if config_snapshot_hash is not None:
                row.config_snapshot_hash = config_snapshot_hash
            session.flush()
            return run_from_row(row)

        return await self._run(_update)

    async def add_item(
        self,
        run_id: RunId,
        item_id: ItemId,
        *,
        status: RunItemStatus | str = RunItemStatus.PENDING,
    ) -> RunItemRecord:
        target = coerce_enum(status, RunItemStatus, "status")

        def _add(session: Session) -> RunItemRecord:
            row = RunItemRow(run_id=run_id, item_id=item_id, status=target.value)
            session.add(row)
            session.flush()
            return run_item_from_row(row)

        try:
            return await self._run(_add)
        except IntegrityError as exc:
            raise translate_integrity_error(exc) from exc

    async def set_item_status(
        self,
        run_id: RunId,
        item_id: ItemId,
        status: RunItemStatus | str,
    ) -> RunItemRecord:
        target = coerce_enum(status, RunItemStatus, "status")

        def _update(session: Session) -> RunItemRecord:
            row = session.get(RunItemRow, (run_id, item_id))
            if row is None:
                raise RecordNotFound(
                    "run item not found",
                    context={"run_id": run_id, "item_id": item_id},
                )
            row.status = target.value
            session.flush()
            return run_item_from_row(row)

        return await self._run(_update)

    async def create_step(
        self,
        run_id: RunId,
        item_id: ItemId,
        capability_id: str,
        capability_version: str,
        *,
        status: StepStatus | str = StepStatus.PENDING,
        step_id: StepId | None = None,
        cache_key: str | None = None,
    ) -> StepRecord:
        target = coerce_enum(status, StepStatus, "status")
        identifier = StepId(step_id) if step_id is not None else StepId(new_uuid7())

        def _create(session: Session) -> StepRecord:
            row = StepRow(
                id=identifier,
                run_id=run_id,
                item_id=item_id,
                capability_id=capability_id,
                capability_version=capability_version,
                cache_key=cache_key,
                status=target.value,
                attempts=0,
                warnings_json="[]",
            )
            session.add(row)
            session.flush()
            return step_from_row(row)

        try:
            return await self._run(_create)
        except IntegrityError as exc:
            raise translate_integrity_error(exc) from exc

    async def update_step(
        self,
        step_id: StepId,
        *,
        status: StepStatus | str | None = None,
        attempts: int | None = None,
        started_at: datetime | None = None,
        finished_at: datetime | None = None,
        error: dict[str, Any] | None = None,
        warnings: tuple[dict[str, Any] | str, ...] | None = None,
        cache_key: str | None = None,
    ) -> StepRecord:
        target = None if status is None else coerce_enum(status, StepStatus, "status")

        def _update(session: Session) -> StepRecord:
            row = session.get(StepRow, step_id)
            if row is None:
                raise RecordNotFound(
                    "step not found",
                    context={"step_id": step_id},
                )
            if target is not None:
                row.status = target.value
            if attempts is not None:
                row.attempts = attempts
            if started_at is not None:
                row.started_at = to_iso_z(started_at)
            if finished_at is not None:
                row.finished_at = to_iso_z(finished_at)
            if error is not None:
                row.error_json = to_json(error)
            if warnings is not None:
                row.warnings_json = to_json(list(warnings))
            if cache_key is not None:
                row.cache_key = cache_key
            session.flush()
            return step_from_row(row)

        return await self._run(_update)

    async def list_steps(
        self,
        run_id: RunId,
        *,
        item_id: ItemId | None = None,
    ) -> list[StepRecord]:
        def _list(session: Session) -> list[StepRecord]:
            statement = select(StepRow).where(StepRow.run_id == run_id)
            if item_id is not None:
                statement = statement.where(StepRow.item_id == item_id)
            statement = statement.order_by(StepRow.id)
            rows = session.scalars(statement).all()
            return [step_from_row(row) for row in rows]

        return await self._run(_list)

    async def list_steps_for_item(self, item_id: ItemId) -> list[StepRecord]:
        def _list(session: Session) -> list[StepRecord]:
            rows = session.scalars(
                select(StepRow).where(StepRow.item_id == item_id).order_by(StepRow.id)
            ).all()
            return [step_from_row(row) for row in rows]

        return await self._run(_list)

    async def list_items(self, run_id: RunId) -> list[RunItemRecord]:
        def _list(session: Session) -> list[RunItemRecord]:
            rows = session.scalars(
                select(RunItemRow).where(RunItemRow.run_id == run_id).order_by(RunItemRow.item_id)
            ).all()
            return [run_item_from_row(row) for row in rows]

        return await self._run(_list)

    async def find_resumable(self) -> list[RunRecord]:
        resumable = (RunStatus.RUNNING.value, RunStatus.PAUSED.value)

        def _find(session: Session) -> list[RunRecord]:
            rows = session.scalars(
                select(RunRow)
                .where(RunRow.status.in_(resumable))
                .order_by(RunRow.started_at, RunRow.id)
            ).all()
            return [run_from_row(row) for row in rows]

        return await self._run(_find)


__all__ = ["SqliteRunRepository"]
