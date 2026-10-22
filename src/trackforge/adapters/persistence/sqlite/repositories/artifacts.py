"""SQLite artifact-index repository."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from trackforge.adapters.persistence.sqlite._errors import translate_integrity_error
from trackforge.adapters.persistence.sqlite._mapping import (
    artifact_from_row,
    optional_iso_z,
    to_iso_z,
    to_json,
)
from trackforge.adapters.persistence.sqlite.models import ArtifactRow
from trackforge.adapters.persistence.sqlite.repositories._base import SqliteRepository
from trackforge.application.dto.db import ArtifactRecord
from trackforge.domain.model.ids import ArtifactId


class SqliteArtifactRepository(SqliteRepository):
    """Store artifact metadata; payload bytes stay in the artifact store."""

    async def get(self, artifact_id: ArtifactId) -> ArtifactRecord | None:
        def _get(session: Session) -> ArtifactRecord | None:
            row = session.get(ArtifactRow, artifact_id)
            return None if row is None else artifact_from_row(row)

        return await self._run(_get)

    async def find_by_cache_key(self, cache_key: str) -> ArtifactRecord | None:
        def _find(session: Session) -> ArtifactRecord | None:
            row = session.scalar(select(ArtifactRow).where(ArtifactRow.cache_key == cache_key))
            return None if row is None else artifact_from_row(row)

        return await self._run(_find)

    async def save(self, artifact: ArtifactRecord) -> ArtifactRecord:
        def _save(session: Session) -> ArtifactRecord:
            row = ArtifactRow(
                id=artifact.id,
                type=artifact.artifact_type,
                schema_version=artifact.schema_version,
                producer_id=artifact.producer_id,
                producer_version=artifact.producer_version,
                cache_key=artifact.cache_key,
                payload_path=artifact.payload_path,
                payload_hash=artifact.payload_hash,
                inputs_json=to_json(list(artifact.inputs)),
                params_json=to_json(artifact.params),
                params_hash=artifact.params_hash,
                warnings_json=to_json(list(artifact.warnings)),
                license=artifact.license,
                created_at=to_iso_z(artifact.created_at),
                expires_at=optional_iso_z(artifact.expires_at),
            )
            session.add(row)
            session.flush()
            return artifact_from_row(row)

        try:
            return await self._run(_save)
        except IntegrityError as exc:
            raise translate_integrity_error(exc) from exc


__all__ = ["SqliteArtifactRepository"]
