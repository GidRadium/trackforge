"""Copy a local candidate into CAS and persist its catalog provenance."""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

from trackforge.application.dto.plan import AcquireOptions
from trackforge.application.ports.acquisition import IntakeResult
from trackforge.application.ports.repositories import UnitOfWork
from trackforge.application.ports.sources import (
    absolute_local_path,
    local_audio_extension,
)
from trackforge.application.ports.storage import ContentAddressedStore, ContentHash
from trackforge.domain.errors import (
    AcquisitionFailed,
    DomainValidationError,
    DuplicateContentHash,
    StorageError,
    UnsupportedSource,
)
from trackforge.domain.model.ids import (
    ArtifactId,
    FileInstanceId,
    IntakeId,
    ItemId,
    MediaId,
    new_uuid7,
)
from trackforge.domain.model.item import Item, ItemStatus
from trackforge.domain.model.media import (
    FileInstance,
    FileRole,
    Intake,
    IntakeStatus,
    MediaContainer,
    MediaObject,
)
from trackforge.domain.model.source import Candidate, LocalPath


class LocalCopyAcquirer:
    """Stream local bytes into the immutable content-addressed store."""

    id = "local_copy"
    version = "1"

    def __init__(
        self,
        cas: ContentAddressedStore,
        unit_of_work_factory: Callable[[], UnitOfWork],
    ) -> None:
        self._cas = cas
        self._uow_factory = unit_of_work_factory

    def supports(self, candidate: Candidate) -> bool:
        if not isinstance(candidate.source_ref, LocalPath):
            return False
        return local_audio_extension(Path(candidate.source_ref.path)) is not None

    async def acquire(
        self,
        candidate: Candidate,
        opts: AcquireOptions | None = None,
    ) -> IntakeResult:
        """Copy ``candidate`` into CAS, never opening the source for writing."""

        if not isinstance(candidate, Candidate):
            raise DomainValidationError("candidate must be a Candidate")
        if not isinstance(candidate.source_ref, LocalPath):
            raise UnsupportedSource(
                "local_copy only acquires local paths",
                user_action="Choose a candidate from a local file source.",
                context={"source_type": type(candidate.source_ref).__name__},
            )

        source_path = absolute_local_path(candidate.source_ref.path)
        extension = local_audio_extension(source_path)
        if extension is None:
            raise UnsupportedSource(
                "unsupported local audio extension",
                user_action="Use one of: .mp3, .m4a, .flac, .ogg, .opus.",
                context={"path": str(source_path), "suffix": source_path.suffix},
            )

        before = self._stat_source(source_path)
        content_hash = await self._copy_to_cas(source_path)
        after = self._stat_source(source_path)
        if (
            before.st_size != after.st_size
            or before.st_mtime_ns != after.st_mtime_ns
            or before.st_ino != after.st_ino
        ):
            raise AcquisitionFailed(
                "source file changed while it was being copied",
                user_action="Wait for the file to stop changing, then retry.",
                context={"path": str(source_path)},
            )

        options = opts or AcquireOptions()
        snapshot_id = (
            ArtifactId(options.source_snapshot_ref)
            if options.source_snapshot_ref is not None
            else None
        )
        async with self._uow_factory() as uow:
            media, reused_media = await self._get_or_create_media(
                uow,
                content_hash=content_hash,
                size_bytes=before.st_size,
                container=MediaContainer(extension.lstrip(".")),
            )

            item = await uow.items.save(
                Item(
                    id=ItemId(new_uuid7()),
                    media_id=media.id,
                    status=ItemStatus.DISCOVERED,
                )
            )
            intake = await uow.intakes.save(
                Intake(
                    id=IntakeId(new_uuid7()),
                    item_id=item.id,
                    source_ref=candidate.source_ref,
                    candidate=candidate,
                    source_snapshot_artifact_id=snapshot_id,
                    downloader=self.id,
                    downloader_version=self.version,
                    media_id=media.id,
                    status=IntakeStatus.SUCCEEDED,
                )
            )
            item.primary_intake_id = intake.id
            item.transition_to(ItemStatus.ACQUIRED)
            item = await uow.items.save(item)
            file_instance = await uow.file_instances.save(
                FileInstance(
                    id=FileInstanceId(new_uuid7()),
                    media_id=media.id,
                    path=str(source_path),
                    role=FileRole.SOURCE,
                    size_bytes=before.st_size,
                    mtime=before.st_mtime,
                    intake_id=intake.id,
                )
            )

        return IntakeResult(
            intake=intake,
            media=media,
            file_instance=file_instance,
            item_id=item.id,
            reused_media=reused_media,
            source_snapshot_ref=options.source_snapshot_ref,
        )

    def _stat_source(self, source_path: Path) -> os.stat_result:
        try:
            if not source_path.exists():
                raise AcquisitionFailed(
                    "source file disappeared before it could be acquired",
                    user_action="Check the path and retry if the file is available.",
                    context={"path": str(source_path)},
                )
            if not source_path.is_file():
                raise UnsupportedSource(
                    "local path is not a regular file",
                    user_action="Pass the path to an existing supported audio file.",
                    context={"path": str(source_path)},
                )
            return source_path.stat()
        except OSError as exc:
            raise AcquisitionFailed(
                "source file cannot be read",
                user_action="Check file permissions and that the file is available.",
                context={"path": str(source_path), "error": str(exc)},
            ) from exc

    async def _copy_to_cas(self, source_path: Path) -> str:
        try:
            content_hash = await self._cas.put_file(source_path)
        except (OSError, StorageError) as exc:
            raise AcquisitionFailed(
                "could not copy the source file into content-addressed storage",
                user_action="Check free disk space and the data/cache directory permissions.",
                context={"path": str(source_path), "error": str(exc)},
            ) from exc
        return str(content_hash)

    async def _get_or_create_media(
        self,
        uow: UnitOfWork,
        *,
        content_hash: str,
        size_bytes: int,
        container: MediaContainer,
    ) -> tuple[MediaObject, bool]:
        existing = await uow.media.find_by_hash(content_hash)
        if existing is not None:
            return existing, True

        candidate = MediaObject(
            id=MediaId(new_uuid7()),
            content_hash=content_hash,
            size_bytes=size_bytes,
            cas_path=str(self._cas.path_for(ContentHash(content_hash))),
            container=container,
        )
        try:
            return await uow.media.save(candidate), False
        except DuplicateContentHash:
            # Another writer won the race; reuse the immutable object.
            existing = await uow.media.find_by_hash(content_hash)
            if existing is None:
                raise
            return existing, True


__all__ = ["LocalCopyAcquirer"]
