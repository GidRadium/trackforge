"""SQLite registry for locally available model artifacts."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from trackforge.adapters.persistence.sqlite._mapping import model_from_row, to_iso_z, to_json
from trackforge.adapters.persistence.sqlite.models import ModelRegistryRow
from trackforge.adapters.persistence.sqlite.repositories._base import SqliteRepository
from trackforge.application.ports.models import ModelInfo


class SqliteModelRepository(SqliteRepository):
    """Persist model availability; weight bytes stay on the filesystem."""

    async def list_models(self) -> list[ModelInfo]:
        def _list(session: Session) -> list[ModelInfo]:
            rows = session.scalars(select(ModelRegistryRow).order_by(ModelRegistryRow.id)).all()
            return [model_from_row(row) for row in rows]

        return await self._run(_list)

    async def get_model(self, model_id: str) -> ModelInfo | None:
        def _get(session: Session) -> ModelInfo | None:
            row = session.get(ModelRegistryRow, model_id)
            return None if row is None else model_from_row(row)

        return await self._run(_get)

    async def upsert_model(self, model: ModelInfo) -> ModelInfo:
        def _upsert(session: Session) -> ModelInfo:
            row = session.get(ModelRegistryRow, model.id)
            if row is None:
                row = ModelRegistryRow(id=model.id)
                session.add(row)
            row.version = model.version
            row.kind = model.kind.value
            row.license = model.license
            row.status = model.status.value
            row.path = model.path
            row.checksum = model.checksum
            row.size_bytes = model.size_bytes
            row.source = model.source
            row.heavy = model.heavy
            row.metadata_json = to_json(model.metadata)
            row.updated_at = to_iso_z(model.updated_at)
            session.flush()
            return model_from_row(row)

        return await self._run(_upsert)


__all__ = ["SqliteModelRepository"]
