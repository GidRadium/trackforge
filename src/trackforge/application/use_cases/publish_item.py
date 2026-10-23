"""Publish a rendered bundle into the managed library, idempotently."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Protocol, cast

from trackforge.application.dto.results import PublishResult
from trackforge.application.ports.publishing import (
    DEFAULT_NAMING_TEMPLATE,
    Publisher,
    PublishOptions,
)
from trackforge.application.ports.repositories import UnitOfWork
from trackforge.domain.errors import (
    DomainValidationError,
    PublishFailed,
    RecordNotFound,
)
from trackforge.domain.model.ids import ItemId, RunId
from trackforge.domain.model.item import ItemStatus
from trackforge.domain.model.outputs import LibraryEntry, LibraryStatus, OutputBundle


class _BundleReader(Protocol):
    async def latest_bundle(self, item_id: ItemId) -> OutputBundle | None: ...


class PublishItem:
    """Copy one item's latest (or selected) bundle and persist its entry."""

    def __init__(
        self,
        unit_of_work_factory: Callable[[], UnitOfWork],
        publisher: Publisher,
        *,
        workspace_root: Path | None = None,
        library_root: Path | None = None,
    ) -> None:
        self._uow_factory = unit_of_work_factory
        self._publisher = publisher
        self._workspace_root = Path(workspace_root) if workspace_root is not None else None
        self._library_root = Path(library_root) if library_root is not None else None

    async def publish(
        self,
        item_id: ItemId,
        options: PublishOptions | None = None,
        *,
        bundle_id: str | None = None,
        library_root: Path | None = None,
        naming: str = DEFAULT_NAMING_TEMPLATE,
        bundle_dir: Path | None = None,
        run_id: RunId | None = None,
        force: bool = False,
    ) -> PublishResult:
        """Publish a bundle; a matching existing entry is returned as-is."""

        target_options = options or PublishOptions()
        target_root = Path(library_root) if library_root is not None else self._library_root
        if target_root is None:
            raise PublishFailed(
                "no library root is configured",
                user_action="Set library_dir in the configuration or pass --library-dir.",
            )
        if not isinstance(target_options, PublishOptions):
            raise DomainValidationError("options must be a PublishOptions instance")

        async with self._uow_factory() as uow:
            item = await uow.items.get(item_id)
            if item is None:
                raise RecordNotFound("item not found", context={"item_id": item_id})
            if item.status not in {
                ItemStatus.RESOLVED,
                ItemStatus.RENDERED,
                ItemStatus.PUBLISHED,
            }:
                raise PublishFailed(
                    "item must be rendered before it can be published",
                    user_action="Render the item first.",
                    context={"item_id": item_id, "status": item.status.value},
                )
            bundle = await _load_bundle(uow, item_id, bundle_id)
            entries = await uow.library.list_entries(item_id=item_id)

        existing = None if force else _matching_entry(entries, bundle)
        if existing is not None:
            return _existing_result(existing, bundle)

        if bundle_dir is not None:
            entry = await self._publisher.publish(
                bundle,
                target_root,
                naming,
                target_options,
                bundle_dir=bundle_dir,
            )
        else:
            # The four-argument contract is the stable port surface.  The
            # filesystem adapter derives its workspace path from its own
            # configured root and the bundle's item/render identifiers.
            entry = await self._publisher.publish(
                bundle,
                target_root,
                naming,
                target_options,
            )
        if entry.item_id != item_id:
            raise PublishFailed(
                "publisher returned an entry for a different item",
                context={
                    "expected_item_id": item_id,
                    "actual_item_id": entry.item_id,
                    "run_id": str(run_id) if run_id is not None else None,
                },
            )
        try:
            async with self._uow_factory() as uow:
                saved = await uow.library.save_entry(entry)
                stored_item = await uow.items.get(item_id)
                if stored_item is not None and stored_item.status is ItemStatus.RENDERED:
                    stored_item.transition_to(ItemStatus.PUBLISHED)
                    await uow.items.save(stored_item)
        except Exception as exc:
            _remove_published_file(entry.path)
            audio = Path(entry.path)
            _remove_published_file(str(audio.with_suffix(".lrc")))
            sidecar = _find_sidecar(audio.parent, bundle)
            if sidecar is not None:
                _remove_published_file(str(sidecar))
            if isinstance(exc, (PublishFailed, DomainValidationError)):
                raise
            raise PublishFailed(
                "published file could not be recorded in the catalog",
                user_action="Check the catalog and retry publication.",
                context={"item_id": item_id, "bundle_id": str(bundle.id)},
            ) from exc
        return _result_for_entry(saved)


async def _load_bundle(
    uow: UnitOfWork,
    item_id: ItemId,
    bundle_id: str | None,
) -> OutputBundle:
    if bundle_id is not None:
        bundle = await uow.renders.get_bundle(bundle_id)
        if bundle is None or bundle.item_id != item_id:
            raise PublishFailed(
                "output bundle not found for item",
                context={"item_id": item_id, "bundle_id": bundle_id},
            )
        return bundle
    bundle = await _latest_bundle(uow, item_id)
    if bundle is None:
        raise PublishFailed(
            "item has no output bundle",
            user_action="Render the item before publishing.",
            context={"item_id": item_id},
        )
    return bundle


async def _latest_bundle(uow: UnitOfWork, item_id: ItemId) -> OutputBundle | None:
    reader = cast(_BundleReader, uow.renders)
    return await reader.latest_bundle(item_id)


def _matching_entry(entries: list[LibraryEntry], bundle: OutputBundle) -> LibraryEntry | None:
    for entry in reversed(entries):
        if str(entry.bundle_id) != str(bundle.id):
            continue
        if entry.status is not LibraryStatus.PUBLISHED:
            continue
        path = Path(entry.path)
        try:
            if path.is_file() and path.stat().st_size > 0:
                return entry
        except OSError:
            continue
    return None


def _existing_result(entry: LibraryEntry, bundle: OutputBundle) -> PublishResult:
    audio = Path(entry.path)
    lrc = audio.with_suffix(".lrc")
    sidecar = _find_sidecar(audio.parent, bundle)
    return PublishResult(
        entry=entry,
        cache_hit=True,
        sidecar_path=str(sidecar) if sidecar is not None else None,
        lrc_path=str(lrc) if lrc.is_file() else None,
    )


def _find_sidecar(directory: Path, bundle: OutputBundle) -> Path | None:
    candidates = sorted(directory.glob("trackforge*.json"))
    for candidate in candidates:
        try:
            document = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        trackforge = document.get("trackforge")
        if (
            isinstance(trackforge, dict)
            and str(trackforge.get("bundle_hash")) == bundle.bundle_hash
        ):
            return candidate
    return None


def _result_for_entry(entry: LibraryEntry) -> PublishResult:
    audio = Path(entry.path)
    lrc = audio.with_suffix(".lrc")
    sidecars = sorted(audio.parent.glob("trackforge*.json"))
    return PublishResult(
        entry=entry,
        sidecar_path=str(sidecars[-1]) if sidecars else None,
        lrc_path=str(lrc) if lrc.is_file() else None,
    )


def _remove_published_file(path: str) -> None:
    try:
        Path(path).unlink(missing_ok=True)
    except OSError:
        pass


__all__ = ["PublishItem"]
