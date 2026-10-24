"""Acquire one yt-dlp candidate into CAS and persist provenance.

The downloader writes into a private temporary directory.  The bytes are then
hashed into CAS, the raw snapshot is stored as an immutable artifact, and only
after that are the catalog records written.  Temporary files are removed even
when the transaction fails; an interrupted run cannot leave a partial output
in the library.
"""

from __future__ import annotations

import shutil
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from trackforge.adapters.http.url_safety import UrlSafetyGuard, sanitize_url
from trackforge.adapters.sources.ytdlp.client import DownloadedFile, DownloadOptions, YtDlpClient
from trackforge.adapters.sources.ytdlp.cookies import CookieResolver
from trackforge.adapters.sources.ytdlp.metadata import build_source_metadata, sanitize_snapshot
from trackforge.adapters.sources.ytdlp.source import select_best_audio
from trackforge.application.dto.artifacts import (
    ArtifactInput,
    ArtifactManifest,
    ArtifactProducer,
)
from trackforge.application.dto.db import ArtifactRecord
from trackforge.application.dto.plan import AcquireOptions
from trackforge.application.ports.acquisition import IntakeResult
from trackforge.application.ports.observations import (
    SOURCE_METADATA_ARTIFACT,
    SOURCE_SNAPSHOT_ARTIFACT,
)
from trackforge.application.ports.repositories import UnitOfWork
from trackforge.application.ports.storage import (
    ArtifactRef,
    ArtifactStore,
    ContentAddressedStore,
    ContentHash,
)
from trackforge.application.services.canonical_json import canonical_json, sha256_hex
from trackforge.domain.errors import (
    AcquisitionFailed,
    DomainValidationError,
    DrmProtected,
    DuplicateContentHash,
    OfflineModeError,
    PreviewOnly,
    StorageError,
    UnsafeUrl,
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
from trackforge.domain.model.source import Candidate, Format, Url

_SNAPSHOT_SCHEMA_VERSION = 1
_METADATA_SCHEMA_VERSION = 1
_PRODUCER_ID = "ytdlp"


class YtDlpAcquirer:
    """Download audio with yt-dlp, verify it, and store it in CAS."""

    id = "ytdlp"
    version = "1"

    def __init__(
        self,
        cas: ContentAddressedStore,
        unit_of_work_factory: Callable[[], UnitOfWork],
        *,
        artifact_store: ArtifactStore | None = None,
        cache_dir: Path | None = None,
        client: YtDlpClient | None = None,
        cookie_dir: Path | None = None,
        cookie_files: Mapping[str, str] | None = None,
        guard: UrlSafetyGuard | None = None,
    ) -> None:
        self._cas = cas
        self._uow_factory = unit_of_work_factory
        self._artifact_store = artifact_store
        self._cache_dir = Path(cache_dir) if cache_dir is not None else None
        self._client = client if client is not None else YtDlpClient()
        self._cookie_dir = Path(cookie_dir) if cookie_dir is not None else None
        self._cookies = CookieResolver.from_mapping(
            self._cookie_dir,
            cookie_files,
        )
        self._guard = guard if guard is not None else UrlSafetyGuard()

    @property
    def client(self) -> YtDlpClient:
        return self._client

    def supports(self, candidate: Candidate) -> bool:
        if not isinstance(candidate.source_ref, Url):
            return False
        if not self._guard.supports(candidate.source_ref.url):
            return False
        try:
            self._guard.validate(candidate.source_ref.url)
        except UnsafeUrl:
            return False
        return True

    async def acquire(
        self,
        candidate: Candidate,
        opts: AcquireOptions | None = None,
    ) -> IntakeResult:
        if not isinstance(candidate, Candidate):
            raise DomainValidationError("candidate must be a Candidate")
        if not isinstance(candidate.source_ref, Url):
            raise UnsupportedSource(
                "ytdlp only acquires URL candidates",
                user_action="Choose a candidate resolved from a supported URL.",
                context={"source_type": type(candidate.source_ref).__name__},
            )
        options = opts or AcquireOptions()
        if options.offline:
            raise OfflineModeError(
                "offline mode does not allow URL acquisition",
                context={"url": sanitize_url(candidate.source_ref.url)},
            )
        url = candidate.source_ref.url
        site_hint = candidate.source_ref.site_hint
        self._guard.validate(url)
        selected = _selected_format(candidate, options)
        if selected.drm:
            raise DrmProtected(
                "selected format is DRM protected",
                context={"format_id": selected.format_id},
            )
        candidate = replace(
            candidate,
            extra={**candidate.extra, "selected_format_id": selected.format_id or ""},
        )
        if not options.force:
            existing = await self._find_existing(candidate, selected)
            if existing is not None:
                return existing

        cookie = self._cookies.select(url, site_hint=site_hint)
        downloader_version = self._client.version()
        workdir = self._new_workdir()
        try:
            downloaded = self._client.download(
                url,
                candidate,
                workdir,
                DownloadOptions(
                    format_id=selected.format_id,
                    cookies_path=cookie.path if cookie is not None else None,
                    cookie_validated=cookie is not None,
                    site_hint=site_hint,
                ),
            )
            _validate_download(downloaded, candidate, selected)
            content_hash = await self._put_cas(downloaded.path)
            snapshot = await self._store_snapshot(
                downloaded,
                content_hash=content_hash,
                candidate=candidate,
                downloader_version=downloader_version,
            )
            metadata = await self._store_metadata(
                downloaded,
                snapshot=snapshot,
                downloader_version=downloader_version,
            )
            return await self._persist(
                candidate=candidate,
                selected=selected,
                downloaded=downloaded,
                content_hash=content_hash,
                snapshot=snapshot,
                metadata=metadata,
                downloader_version=downloader_version,
            )
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    def _new_workdir(self) -> Path:
        try:
            if self._cache_dir is None:
                return Path(tempfile.mkdtemp(prefix="trackforge-ytdlp-"))
            root = self._cache_dir / "tmp"
            workdir = root / f"ytdlp-{new_uuid7()}"
            workdir.mkdir(parents=True, exist_ok=True, mode=0o700)
            return workdir
        except OSError as exc:
            raise AcquisitionFailed(
                "download workspace could not be created",
                user_action="Check cache directory permissions and free space.",
                context={"path": str(self._cache_dir), "error": str(exc)},
            ) from exc

    async def _put_cas(self, path: Path) -> str:
        try:
            content_hash = await self._cas.put_file(path)
        except (OSError, StorageError) as exc:
            raise AcquisitionFailed(
                "downloaded file could not be stored in CAS",
                user_action="Check free disk space and data/cache permissions.",
                context={"error": str(exc)},
            ) from exc
        return str(content_hash)

    async def _store_snapshot(
        self,
        downloaded: DownloadedFile,
        *,
        content_hash: str,
        candidate: Candidate,
        downloader_version: str,
    ) -> _StoredArtifact | None:
        if self._artifact_store is None:
            return None
        raw = sanitize_snapshot(downloaded.info_json)
        if not isinstance(raw, Mapping):
            raw = {}
        payload = canonical_json(dict(raw))
        safe_url = (
            sanitize_url(candidate.source_ref.url)
            if isinstance(candidate.source_ref, Url)
            else None
        )
        cache_key = sha256_hex(
            canonical_json(
                {
                    "type": str(SOURCE_SNAPSHOT_ARTIFACT),
                    "downloader_version": downloader_version,
                    "url": safe_url,
                    "payload_hash": sha256_hex(payload),
                }
            )
        )
        manifest = ArtifactManifest(
            artifact_id=new_uuid7(),
            type=str(SOURCE_SNAPSHOT_ARTIFACT),
            schema_version=_SNAPSHOT_SCHEMA_VERSION,
            producer=ArtifactProducer(id=_PRODUCER_ID, version=downloader_version),
            inputs=[ArtifactInput(type="media.raw.v1", hash=content_hash)],
            params={
                "url": safe_url,
                "external_id": candidate.external_id,
                "extractor": str(raw.get("extractor") or ""),
            },
            warnings=[],
            created_at=_iso_z(datetime.now(UTC)),
        )
        return await self._store_artifact(cache_key, manifest, payload, SOURCE_SNAPSHOT_ARTIFACT)

    async def _store_metadata(
        self,
        downloaded: DownloadedFile,
        *,
        snapshot: _StoredArtifact | None,
        downloader_version: str,
    ) -> _StoredArtifact | None:
        if self._artifact_store is None:
            return None
        normalized = build_source_metadata(downloaded.info_json).model_dump(mode="json")
        payload = canonical_json(normalized)
        cache_key = sha256_hex(
            canonical_json(
                {
                    "type": str(SOURCE_METADATA_ARTIFACT),
                    "snapshot": snapshot.ref.cache_key if snapshot is not None else "",
                    "payload_hash": sha256_hex(payload),
                }
            )
        )
        manifest = ArtifactManifest(
            artifact_id=new_uuid7(),
            type=str(SOURCE_METADATA_ARTIFACT),
            schema_version=_METADATA_SCHEMA_VERSION,
            producer=ArtifactProducer(id=_PRODUCER_ID, version=downloader_version),
            inputs=(
                [
                    ArtifactInput(
                        type=str(SOURCE_SNAPSHOT_ARTIFACT),
                        hash=snapshot.ref.payload_hash,
                    )
                ]
                if snapshot is not None
                else []
            ),
            params={
                "external_id": normalized["external_id"],
                "extractor": normalized.get("extractor") or "",
            },
            warnings=[],
            created_at=_iso_z(datetime.now(UTC)),
        )
        return await self._store_artifact(cache_key, manifest, payload, SOURCE_METADATA_ARTIFACT)

    async def _store_artifact(
        self,
        cache_key: str,
        manifest: ArtifactManifest,
        payload: bytes,
        artifact_type: Any,
    ) -> _StoredArtifact:
        store = self._artifact_store
        if store is None:  # pragma: no cover - guarded by callers
            raise AcquisitionFailed("artifact store is required for source snapshots")
        try:
            ref = await store.put(cache_key, manifest, payload)
            stored_manifest, _ = await store.load(ref)
        except (OSError, StorageError) as exc:
            raise AcquisitionFailed(
                f"could not store {artifact_type} artifact",
                context={"error": str(exc)},
            ) from exc
        return _StoredArtifact(ref=ref, manifest=stored_manifest)

    async def _persist(
        self,
        *,
        candidate: Candidate,
        selected: Format,
        downloaded: DownloadedFile,
        content_hash: str,
        snapshot: _StoredArtifact | None,
        metadata: _StoredArtifact | None,
        downloader_version: str,
    ) -> IntakeResult:
        container = _container(downloaded.container)
        async with self._uow_factory() as uow:
            if snapshot is not None:
                await self._index_artifact(uow, snapshot)
            if metadata is not None:
                await self._index_artifact(uow, metadata)
            media, reused_media = await self._get_or_create_media(
                uow,
                content_hash=content_hash,
                size_bytes=downloaded.size_bytes,
                container=container,
                selected=selected,
                duration=downloaded.duration,
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
                    source_snapshot_artifact_id=(
                        ArtifactId(snapshot.manifest.artifact_id) if snapshot is not None else None
                    ),
                    downloader=self.id,
                    downloader_version=downloader_version,
                    media_id=media.id,
                    status=IntakeStatus.SUCCEEDED,
                )
            )
            item.primary_intake_id = intake.id
            item.transition_to(ItemStatus.ACQUIRED)
            item = await uow.items.save(item)
            cas_path = Path(media.cas_path)
            try:
                mtime = cas_path.stat().st_mtime
            except OSError:
                mtime = datetime.now(UTC).timestamp()
            file_instance = await uow.file_instances.save(
                FileInstance(
                    id=FileInstanceId(new_uuid7()),
                    media_id=media.id,
                    path=str(cas_path),
                    role=FileRole.CAS,
                    size_bytes=downloaded.size_bytes,
                    mtime=mtime,
                    intake_id=intake.id,
                )
            )
        return IntakeResult(
            intake=intake,
            media=media,
            file_instance=file_instance,
            item_id=item.id,
            reused_media=reused_media,
            source_snapshot_ref=(
                str(snapshot.manifest.artifact_id) if snapshot is not None else None
            ),
        )

    @staticmethod
    async def _index_artifact(uow: UnitOfWork, stored: _StoredArtifact) -> None:
        existing = await uow.artifacts.find_by_cache_key(stored.ref.cache_key)
        if existing is not None:
            return
        await uow.artifacts.save(
            ArtifactRecord.from_manifest(
                stored.manifest,
                cache_key=stored.ref.cache_key,
                payload_path=str(stored.ref.payload_path),
            )
        )

    async def _get_or_create_media(
        self,
        uow: UnitOfWork,
        *,
        content_hash: str,
        size_bytes: int,
        container: MediaContainer,
        selected: Format,
        duration: float | None,
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
            codec=selected.codec,
            bitrate=selected.bitrate,
            sample_rate=selected.sample_rate,
            channels=selected.channels,
            duration_seconds=duration,
        )
        try:
            return await uow.media.save(candidate), False
        except DuplicateContentHash:
            existing = await uow.media.find_by_hash(content_hash)
            if existing is None:
                raise
            return existing, True

    async def _find_existing(
        self,
        candidate: Candidate,
        selected: Format,
    ) -> IntakeResult | None:
        if not isinstance(candidate.source_ref, Url):
            return None
        async with self._uow_factory() as uow:
            finder = getattr(uow.intakes, "find_by_source_ref", None)
            if not callable(finder):
                return None
            intakes = await finder(candidate.source_ref)
            for intake in reversed(tuple(intakes)):
                if intake.status is not IntakeStatus.SUCCEEDED:
                    continue
                if _intake_format_id(intake) != (selected.format_id or ""):
                    continue
                item = await uow.items.get(intake.item_id)
                if item is None or item.media_id is None:
                    continue
                media = await uow.media.get(item.media_id)
                if media is None or not Path(media.cas_path).is_file():
                    continue
                instances = await uow.file_instances.find_by_media(media.id)
                instance = next(
                    (record for record in instances if record.intake_id == intake.id),
                    None,
                )
                if instance is None:
                    continue
                return IntakeResult(
                    intake=intake,
                    media=media,
                    file_instance=instance,
                    item_id=item.id,
                    reused_media=True,
                    source_snapshot_ref=(
                        str(intake.source_snapshot_artifact_id)
                        if intake.source_snapshot_artifact_id is not None
                        else None
                    ),
                )
        return None


class _StoredArtifact:
    """A stored artifact and the manifest returned by the store."""

    __slots__ = ("manifest", "ref")

    def __init__(self, *, ref: ArtifactRef, manifest: ArtifactManifest) -> None:
        self.ref = ref
        self.manifest = manifest


def _selected_format(candidate: Candidate, options: AcquireOptions) -> Format:
    if options.format_id is not None:
        selected = next(
            (fmt for fmt in candidate.formats if fmt.format_id == options.format_id),
            None,
        )
        if selected is None:
            raise DomainValidationError(
                "selected format is not present in the candidate",
                context={"format_id": options.format_id},
            )
        return selected
    selected_id = candidate.extra.get("selected_format_id")
    if isinstance(selected_id, str) and selected_id:
        selected = next(
            (fmt for fmt in candidate.formats if fmt.format_id == selected_id),
            None,
        )
        if selected is not None:
            return selected
    if not candidate.formats:
        # Flat playlist enumeration intentionally omits formats.  yt-dlp still
        # downloads the configured bestaudio selector; the downloaded
        # snapshot supplies the concrete format facts afterwards.
        return Format()
    return select_best_audio(candidate.formats)


def _validate_download(
    downloaded: DownloadedFile,
    candidate: Candidate,
    selected: Format,
) -> None:
    if downloaded.size_bytes <= 0:
        raise AcquisitionFailed("downloaded file is empty")
    if selected.size is not None and selected.size > 0:
        ratio = downloaded.size_bytes / selected.size
        if ratio < 0.5 and selected.size > 100_000:
            raise PreviewOnly(
                "downloaded file is much smaller than the advertised format",
                context={
                    "downloaded_bytes": downloaded.size_bytes,
                    "expected_bytes": selected.size,
                },
            )
    expected_duration = candidate.duration_seconds
    actual_duration = downloaded.duration
    if (
        expected_duration is not None
        and expected_duration > 30
        and actual_duration is not None
        and actual_duration < expected_duration * 0.5
    ):
        raise PreviewOnly(
            "downloaded duration is much shorter than the source metadata",
            context={
                "downloaded_duration": actual_duration,
                "expected_duration": expected_duration,
            },
        )


def _container(value: str) -> MediaContainer:
    text = (value or "").strip().lower().lstrip(".")
    aliases = {"m4a": "m4a", "mp4": "m4a", "webm": "webm", "opus": "opus", "ogg": "ogg"}
    try:
        return MediaContainer(text)
    except ValueError:
        alias = aliases.get(text)
        if alias is not None:
            try:
                return MediaContainer(alias)
            except ValueError:
                pass
    raise AcquisitionFailed(
        "downloaded container is not supported",
        user_action="Choose an audio-only format with a supported container.",
        context={"container": value},
    )


def _intake_format_id(intake: Intake) -> str:
    value = intake.candidate.extra.get("selected_format_id")
    return value if isinstance(value, str) else ""


def _iso_z(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


__all__ = ["YtDlpAcquirer"]
