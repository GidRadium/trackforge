"""SQLite media-object repository."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from trackforge.adapters.persistence.sqlite._errors import translate_integrity_error
from trackforge.adapters.persistence.sqlite._mapping import media_from_row, to_iso_z
from trackforge.adapters.persistence.sqlite.models import MediaObjectRow
from trackforge.adapters.persistence.sqlite.repositories._base import SqliteRepository
from trackforge.domain.errors import DuplicateContentHash
from trackforge.domain.model.ids import MediaId
from trackforge.domain.model.media import MediaObject


class SqliteMediaRepository(SqliteRepository):
    """Persist immutable media objects keyed by content hash."""

    async def get(self, media_id: MediaId) -> MediaObject | None:
        def _get(session: Session) -> MediaObject | None:
            row = session.get(MediaObjectRow, media_id)
            return None if row is None else media_from_row(row)

        return await self._run(_get)

    async def find_by_hash(self, content_hash: str) -> MediaObject | None:
        def _find(session: Session) -> MediaObject | None:
            row = session.scalar(
                select(MediaObjectRow).where(MediaObjectRow.content_hash == content_hash)
            )
            return None if row is None else media_from_row(row)

        return await self._run(_find)

    async def save(self, media: MediaObject) -> MediaObject:
        def _save(session: Session) -> MediaObject:
            if session.get(MediaObjectRow, media.id) is not None:
                raise DuplicateContentHash(
                    "a media object with this id already exists",
                    context={"media_id": media.id},
                )
            row = MediaObjectRow(
                id=media.id,
                content_hash=media.content_hash,
                size_bytes=media.size_bytes,
                cas_path=media.cas_path,
                container=media.container.value,
                codec=media.codec,
                bitrate=media.bitrate,
                sample_rate=media.sample_rate,
                channels=media.channels,
                duration=media.duration_seconds,
                probe_artifact_id=media.probe_artifact_id,
                created_at=to_iso_z(media.created_at),
            )
            session.add(row)
            session.flush()
            return media_from_row(row)

        try:
            return await self._run(_save)
        except IntegrityError as exc:
            raise translate_integrity_error(exc) from exc


__all__ = ["SqliteMediaRepository"]
