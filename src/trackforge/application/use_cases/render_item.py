"""Render resolved decisions into a verified output bundle.

The use case is the transaction boundary between immutable CAS bytes, the
render/tag adapters, the item workspace, and the catalog.  A bundle is only
persisted after read-back succeeds; failed renders leave no ``renders`` or
``output_bundles`` rows behind.
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from trackforge.application.dto.rendering import (
    COVER_SIDECAR_NAME,
    RENDER_NAMING_TEMPLATE,
    SIDECAR_NAME,
    TAG_MAPPING_VERSION,
    CoverArt,
    RenderOptions,
    RenderResult,
    TagSet,
    extension_for,
    is_supported_container,
    is_synced_lyrics,
    lyrics_sidecar_name,
    target_format_for,
)
from trackforge.application.ports.rendering import Renderer, TagReader, TagWriter
from trackforge.application.ports.repositories import UnitOfWork
from trackforge.application.ports.storage import (
    ContentAddressedStore,
    is_sha256_hex,
    require_content_hash,
)
from trackforge.application.services.sidecar import (
    SidecarArtifacts,
    SidecarCollection,
    SidecarData,
    SidecarIdentity,
    SidecarSource,
    build_sidecar_document,
    compute_bundle_hash,
    finalize_sidecar,
    sha256_file,
    write_sidecar,
)
from trackforge.domain.errors import (
    DomainValidationError,
    RecordNotFound,
    RenderFailed,
)
from trackforge.domain.model._validation import ensure_datetime, utc_now
from trackforge.domain.model.claims import Claim, ClaimStatus, Subject
from trackforge.domain.model.decisions import DecisionRecord
from trackforge.domain.model.ids import (
    BundleId,
    DecisionRecordId,
    ItemId,
    RenderId,
    new_uuid7,
)
from trackforge.domain.model.item import ItemStatus
from trackforge.domain.model.media import Intake, MediaObject
from trackforge.domain.model.outputs import (
    OutputBundle,
    Render,
    RenderStatus,
    ResolvedMetadata,
)
from trackforge.domain.model.source import (
    CollectionRef,
    LocalPath,
    TextQuery,
    Url,
)

_ID_COMPONENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_TRANSFORMATION_FIELDS = frozenset({"transformation", "transformations"})


class RenderItem:
    """Materialize one item's current decision record into a workspace bundle."""

    def __init__(
        self,
        unit_of_work_factory: Callable[[], UnitOfWork],
        renderer: Renderer,
        tag_writer: TagWriter,
        tag_reader: TagReader,
        workspace_root: Path,
        *,
        cover_loader: Callable[[Path], CoverArt | None] | None = None,
        cas: ContentAddressedStore | None = None,
        clock: Callable[[], datetime] = utc_now,
        id_factory: Callable[[], str] = new_uuid7,
    ) -> None:
        self._uow_factory = unit_of_work_factory
        self._renderer = renderer
        self._tag_writer = tag_writer
        self._tag_reader = tag_reader
        self._workspace_root = Path(workspace_root)
        self._cover_loader = cover_loader
        self._cas = cas
        self._clock = clock
        self._id_factory = id_factory

    async def render(
        self,
        item_id: ItemId,
        options: RenderOptions | None = None,
        *,
        cover: CoverArt | None = None,
        cover_path: Path | None = None,
        decision_record_id: DecisionRecordId | str | None = None,
        render_id: RenderId | str | None = None,
        bundle_id: BundleId | str | None = None,
        created_at: datetime | None = None,
        collection: SidecarCollection | None = None,
        tag_basis: str | None = None,
        tag_alternatives: Mapping[str, Any] | None = None,
        final_warnings: Sequence[str] = (),
    ) -> RenderResult:
        """Render, tag, verify, and persist one output bundle."""

        target_options = options or RenderOptions()
        if not isinstance(target_options, RenderOptions):
            raise RenderFailed("options must be a RenderOptions instance")
        now = ensure_datetime(created_at, "created_at") if created_at else self._clock()
        if now.tzinfo is None:
            now = now.replace(tzinfo=UTC)
        item_component = _id_component(item_id, "item_id")
        render_identifier = _id_component(render_id or self._id_factory(), "render_id")
        bundle_identifier = _id_component(bundle_id or self._id_factory(), "bundle_id")

        async with self._uow_factory() as uow:
            item = await uow.items.get(item_id)
            if item is None:
                raise RecordNotFound("item not found", context={"item_id": item_id})
            if item.status not in {
                ItemStatus.RESOLVED,
                ItemStatus.RENDERED,
                ItemStatus.PUBLISHED,
            }:
                raise RenderFailed(
                    "item must be resolved before it can be rendered",
                    user_action="Resolve the item first.",
                    context={"item_id": item_id, "status": item.status.value},
                )
            if item.media_id is None:
                raise RenderFailed(
                    "item has no acquired media object",
                    context={"item_id": item_id},
                )
            media = await uow.media.get(item.media_id)
            if media is None:
                raise RecordNotFound(
                    "media object not found for item",
                    context={"item_id": item_id, "media_id": item.media_id},
                )
            record = await _load_record(
                uow,
                item.id,
                item.decision_record_id,
                decision_record_id,
            )
            claims = await uow.claims.list_by_item(item.id)
            intakes = await uow.intakes.find_by_item(item.id)

        resolved = _resolved_metadata(record)
        derivative = _derivatives(claims)
        warnings: list[str] = list(
            dict.fromkeys((*resolved.warnings, *(str(item) for item in final_warnings)))
        )
        if cover_path is None and self._cas is not None:
            artwork = resolved.fields.get("artwork")
            if isinstance(artwork, str) and is_sha256_hex(artwork):
                candidate_path = self._cas.path_for(require_content_hash(artwork))
                if candidate_path.is_file():
                    cover_path = candidate_path
        try:
            tagset, lrc_lyrics, elrc_lyrics = _tagset_for(resolved, derivative)
        except DomainValidationError as exc:
            raise RenderFailed(
                "resolved metadata cannot be mapped to canonical tags",
                user_action="Review the decision record for invalid field values.",
                context={"item_id": item_id, "error": exc.message},
            ) from exc

        loaded_cover = cover
        if loaded_cover is None and cover_path is not None:
            try:
                loaded_cover = CoverArt.from_path(cover_path)
            except (OSError, DomainValidationError) as exc:
                raise RenderFailed(
                    "cover art could not be loaded",
                    user_action="Use a JPEG or PNG cover smaller than 10 MB.",
                    context={"path": str(cover_path), "error": str(exc)},
                ) from exc
        if loaded_cover is None and self._cover_loader is not None:
            try:
                loaded_cover = self._cover_loader(Path(media.cas_path))
            except Exception:
                loaded_cover = None
                warnings.append("cover_read_failed")
        if loaded_cover is None:
            warnings.append("cover_missing")
        if (lrc_lyrics or elrc_lyrics) and not target_options.lyrics_sidecar:
            warnings.append("lyrics_sidecar_disabled")

        target = target_format_for(media.container, target_options.target_format)
        extension = extension_for(target)
        if target_options.target_format is None and not is_supported_container(media.container):
            warnings.append(f"target_format_fallback:{target.value}")

        audio_relative = f"track.{extension}"
        renders_root = self._workspace_root / "items" / item_component / "renders"
        final_directory = _safe_child(renders_root, render_identifier)
        temporary_directory = _safe_child(
            renders_root,
            f".{render_identifier}-{_id_component(self._id_factory(), 'render_temp_id')}.tmp",
        )
        committed = False
        try:
            renders_root.mkdir(parents=True, exist_ok=True)
            if final_directory.exists():
                raise RenderFailed(
                    "render workspace already exists",
                    user_action="Use a new render id.",
                    context={"render_id": render_identifier},
                )
            temporary_directory.mkdir(parents=True, mode=0o700)
            rendered = await asyncio.to_thread(
                self._renderer.render,
                media,
                resolved,
                target_options,
                temporary_directory,
            )
            audio_path = Path(rendered.path)
            _require_within(temporary_directory, audio_path)
            if not audio_path.is_file() or audio_path.stat().st_size == 0:
                raise RenderFailed(
                    "renderer produced no audio file",
                    context={"path": str(audio_path)},
                )
            if _same_file(audio_path, Path(media.cas_path)):
                raise RenderFailed("renderer wrote over the source media object")

            embedded_cover = loaded_cover if target_options.cover_embed else None
            await asyncio.to_thread(
                self._tag_writer.write,
                audio_path,
                tagset,
                embedded_cover,
                target_options,
            )
            mismatches = await asyncio.to_thread(
                self._tag_writer.verify,
                audio_path,
                tagset,
                embedded_cover,
            )
            if mismatches:
                raise RenderFailed(
                    "tag read-back did not confirm the canonical values",
                    user_action="Retry the render; the source tags or file may be damaged.",
                    context={
                        "item_id": item_id,
                        "render_id": render_identifier,
                        "mismatches": [mismatch.message() for mismatch in mismatches],
                    },
                )
            # Confirm the generic reader can parse the tagged file too.  The
            # writer's verify is authoritative; this catches adapter drift.
            try:
                await asyncio.to_thread(self._tag_reader.read, audio_path)
            except Exception as exc:
                raise RenderFailed(
                    "rendered file could not be read back",
                    user_action="Retry the render; the output audio may be damaged.",
                    context={"path": str(audio_path), "error": str(exc)},
                ) from exc

            file_hashes: list[tuple[str, str]] = [(audio_relative, sha256_file(audio_path))]
            if target_options.cover_sidecar and loaded_cover is not None:
                cover_path_final = temporary_directory / COVER_SIDECAR_NAME
                _write_bytes(cover_path_final, loaded_cover.data)
                file_hashes.append((COVER_SIDECAR_NAME, sha256_file(cover_path_final)))
            if target_options.lyrics_sidecar and lrc_lyrics:
                lyrics_relative = lyrics_sidecar_name(audio_relative)
                lyrics_path = temporary_directory / lyrics_relative
                _write_text(lyrics_path, lrc_lyrics)
                file_hashes.append((lyrics_relative, sha256_file(lyrics_path)))
            if target_options.lyrics_sidecar and elrc_lyrics:
                elrc_relative = lyrics_sidecar_name(audio_relative, enhanced=True)
                elrc_path = temporary_directory / elrc_relative
                _write_text(elrc_path, elrc_lyrics)
                file_hashes.append((elrc_relative, sha256_file(elrc_path)))

            sidecar_data = SidecarData(
                item_id=item_id,
                content_hash=media.content_hash,
                decision_record_id=record.id,
                created_at=now,
                source=_sidecar_source(intakes, resolved, media),
                metadata=tagset.metadata_document(),
                identity=SidecarIdentity(
                    kind=item.kind.value if item.kind is not None else None,
                    derivative=tuple(derivative),
                    recording_mbid=tagset.mb_recording_id,
                    release_mbid=tagset.mb_release_id,
                ),
                artifacts=SidecarArtifacts(probe=media.probe_artifact_id),
                warnings=tuple(dict.fromkeys(warnings)),
                unknowns=resolved.unknowns,
                collection=collection,
                tag_basis=tag_basis,
                tag_alternatives=dict(tag_alternatives or {}),
            )
            placeholder = build_sidecar_document(sidecar_data)
            bundle_hash = compute_bundle_hash(file_hashes, placeholder)
            final_sidecar = finalize_sidecar(placeholder, bundle_hash)
            write_sidecar(temporary_directory / SIDECAR_NAME, final_sidecar)
            file_hashes.append((SIDECAR_NAME, sha256_file(temporary_directory / SIDECAR_NAME)))

            _fsync_directory(temporary_directory)
            os.replace(temporary_directory, final_directory)
            _fsync_directory(renders_root)
            committed = True

            render = Render(
                id=RenderId(render_identifier),
                item_id=item_id,
                decision_record_id=record.id,
                target_format=target,
                naming_template=RENDER_NAMING_TEMPLATE.format(ext=extension),
                tag_mapping_version=TAG_MAPPING_VERSION,
                options=target_options.as_mapping(),
                status=RenderStatus.SUCCEEDED,
                created_at=now,
            )
            bundle = OutputBundle(
                id=BundleId(bundle_identifier),
                render_id=render.id,
                item_id=item_id,
                files=tuple(sorted(path for path, _ in file_hashes)),
                bundle_hash=bundle_hash,
                created_at=now,
            )
            try:
                async with self._uow_factory() as uow:
                    await uow.renders.save_render(render)
                    await uow.renders.save_bundle(bundle)
                    stored_item = await uow.items.get(item_id)
                    if stored_item is not None and stored_item.status is ItemStatus.RESOLVED:
                        transition_at = (
                            now if now >= stored_item.created_at else stored_item.created_at
                        )
                        stored_item.transition_to(ItemStatus.RENDERED, at=transition_at)
                        await uow.items.save(stored_item)
            except Exception:
                shutil.rmtree(final_directory, ignore_errors=True)
                committed = False
                raise

            return RenderResult(
                render=render,
                bundle=bundle,
                directory=final_directory,
                tagset=tagset,
                warnings=tuple(dict.fromkeys(warnings)),
                created_at=now,
            )
        except RenderFailed:
            raise
        except (OSError, ValueError) as exc:
            raise RenderFailed(
                "render workspace could not be completed",
                user_action="Check workspace permissions and available space.",
                context={"render_id": render_identifier, "error": str(exc)},
            ) from exc
        finally:
            if not committed:
                shutil.rmtree(temporary_directory, ignore_errors=True)


async def _load_record(
    uow: UnitOfWork,
    item_id: ItemId,
    current_record_id: DecisionRecordId | None,
    requested: DecisionRecordId | str | None,
) -> DecisionRecord:
    if requested is not None:
        record = await uow.decisions.get_record(DecisionRecordId(str(requested)))
        if record is None or record.item_id != item_id:
            raise RenderFailed(
                "decision record not found for item",
                context={"item_id": item_id, "decision_record_id": str(requested)},
            )
        return record
    if current_record_id is not None:
        record = await uow.decisions.get_record(current_record_id)
        if record is not None and record.item_id == item_id:
            return record
    records = await uow.decisions.list_records(item_id)
    if not records:
        raise RenderFailed(
            "item has no decision record",
            user_action="Resolve the item before rendering.",
            context={"item_id": item_id},
        )
    return records[-1]


def _resolved_metadata(record: DecisionRecord) -> ResolvedMetadata:
    item_decisions = [decision for decision in record.decisions if decision.subject is Subject.ITEM]
    fields = {decision.field: decision.value for decision in item_decisions}
    unknowns = tuple(decision.field for decision in item_decisions if decision.value is None)
    return ResolvedMetadata(
        item_id=record.item_id,
        decision_record_id=record.id,
        fields=fields,
        unknowns=unknowns,
    )


def _derivatives(claims: Sequence[Claim]) -> tuple[str, ...]:
    result: list[str] = []
    for claim in claims:
        if claim.status not in {ClaimStatus.PROPOSED, ClaimStatus.ACCEPTED}:
            continue
        if claim.subject is not Subject.DERIVATIVE or claim.field not in _TRANSFORMATION_FIELDS:
            continue
        value = claim.value
        if isinstance(value, str):
            values = [value]
        elif isinstance(value, (list, tuple)):
            values = [item for item in value if isinstance(item, str)]
        else:
            continue
        for item in values:
            text = item.strip()
            if text and text not in result:
                result.append(text)
    return tuple(result)


def _tagset_for(
    resolved: ResolvedMetadata,
    derivative: Sequence[str],
) -> tuple[TagSet, str | None, str | None]:
    raw_lyrics = resolved.fields.get("lyrics")
    lyrics = raw_lyrics if isinstance(raw_lyrics, str) and raw_lyrics.strip() else None
    raw_synced = resolved.fields.get("synced_lyrics")
    synced = raw_synced if isinstance(raw_synced, str) and raw_synced.strip() else None
    if synced is None and is_synced_lyrics(lyrics):
        synced = lyrics
        lyrics = None
    plain = None if is_synced_lyrics(lyrics) else lyrics
    raw_elrc = resolved.fields.get("elrc")
    elrc = raw_elrc if isinstance(raw_elrc, str) and raw_elrc.strip() else None
    tagset = TagSet.from_resolved(resolved, derivative=derivative).with_lyrics(plain)
    return tagset, synced, elrc


def _sidecar_source(
    intakes: Sequence[Intake],
    resolved: ResolvedMetadata,
    media: MediaObject,
) -> SidecarSource:
    intake = next(
        (candidate for candidate in intakes if candidate.media_id == media.id),
        intakes[0] if intakes else None,
    )
    if intake is not None:
        ref = intake.source_ref
        candidate_id = intake.candidate.external_id
        if isinstance(ref, LocalPath):
            return SidecarSource(extractor="local", source_id=ref.path)
        if isinstance(ref, Url):
            return SidecarSource(
                url=ref.url,
                extractor=ref.site_hint or _host(ref.url) or "url",
                source_id=candidate_id,
            )
        if isinstance(ref, TextQuery):
            return SidecarSource(extractor="text_query", source_id=candidate_id)
        if isinstance(ref, CollectionRef):
            return SidecarSource(extractor="collection", source_id=candidate_id)
    source_url = resolved.fields.get("source_url")
    source = resolved.fields.get("source")
    source_id = resolved.fields.get("source_id")
    resolved_url = _url_or_none(source_url, source)
    resolved_id = source_id if isinstance(source_id, str) else None
    return SidecarSource(
        url=resolved_url,
        extractor="resolution" if resolved_url is not None or resolved_id is not None else None,
        source_id=resolved_id,
    )


def _url_or_none(source_url: object, source: object) -> str | None:
    if isinstance(source_url, str) and source_url.strip():
        return source_url
    if isinstance(source, str) and source.startswith(("http://", "https://")):
        return source
    return None


def _host(url: str) -> str | None:
    from urllib.parse import urlsplit

    try:
        return urlsplit(url).hostname
    except ValueError:
        return None


def _id_component(value: object, field_name: str) -> str:
    text = str(value).strip()
    if not _ID_COMPONENT_RE.fullmatch(text):
        raise RenderFailed(
            f"{field_name} must be a safe path component",
            context={"field": field_name, "value": text},
        )
    return text


def _safe_child(root: Path, name: str) -> Path:
    candidate = root / name
    _require_within(root, candidate)
    return candidate


def _require_within(root: Path, path: Path) -> None:
    resolved_root = root.resolve()
    resolved_path = path.resolve()
    try:
        resolved_path.relative_to(resolved_root)
    except ValueError as exc:
        raise RenderFailed(
            "render path escaped its workspace root",
            context={"root": str(resolved_root), "path": str(resolved_path)},
        ) from exc


def _same_file(first: Path, second: Path) -> bool:
    try:
        return first.resolve() == second.resolve() or (
            second.exists() and os.path.samefile(first, second)
        )
    except OSError:
        return False


def _write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = _mkstemp(path)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _write_text(path: Path, text: str) -> None:
    _write_bytes(path, text.encode("utf-8"))


def _mkstemp(path: Path) -> tuple[int, str]:
    import tempfile

    return tempfile.mkstemp(prefix=f".{path.name}-", suffix=".tmp", dir=path.parent)


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


__all__ = ["RenderItem"]
