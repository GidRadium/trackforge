"""Plan a playlist/folder collection without acquiring media."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from typing import Any

from trackforge.application.dto.collections import (
    MAX_COLLECTION_ITEMS,
    CollectionPlanOptions,
    CollectionPlanView,
    parse_positions,
)
from trackforge.application.ports.sources import CollectionResolver
from trackforge.application.services.tag_basis import choose_tag_basis
from trackforge.domain.errors import (
    CollectionTooLarge,
    DomainValidationError,
    OfflineModeError,
    UnsupportedSource,
)
from trackforge.domain.model.capabilities import Cost
from trackforge.domain.model.collection import Collection, CollectionItem, TagBasis
from trackforge.domain.model.source import SourceRef, Url


class PlanCollection:
    """Enumerate a collection and apply range/item/skip selection rules."""

    def __init__(self, resolvers: Sequence[CollectionResolver] = ()) -> None:
        self._resolvers = tuple(resolvers)

    async def plan(
        self,
        source: SourceRef,
        options: CollectionPlanOptions | None = None,
    ) -> CollectionPlanView:
        opts = options or CollectionPlanOptions()
        if opts.offline and isinstance(source, Url):
            raise OfflineModeError(
                "offline mode does not allow playlist resolution",
                context={"url": source.url},
            )
        resolver = next(
            (candidate for candidate in self._resolvers if _supports(candidate, source)),
            None,
        )
        if resolver is None:
            raise UnsupportedSource(
                "no installed collection resolver supports this input",
                user_action="Pass a playlist/album URL or a folder of supported audio files.",
                context={"source_type": type(source).__name__},
            )
        resolved = await _resolve(resolver, source, opts.source_options())
        selected, warnings = _select(resolved, opts)
        basis = choose_tag_basis(opts.tag_basis, auto=True)
        warnings = (*warnings, *basis.warnings)
        collection = _with_basis(resolved, basis.basis)
        estimated = _estimate(collection, selected)
        return CollectionPlanView(
            collection=collection,
            selected_items=selected,
            estimated_cost=estimated,
            warnings=tuple(dict.fromkeys(warnings)),
            limits={
                "max_items": opts.max_items,
                "total_items": len(collection.items),
                "selected_items": len(selected),
            },
            resolver_id=resolver.id,
        )


def _supports(resolver: CollectionResolver, source: SourceRef) -> bool:
    collection_method = getattr(resolver, "supports_collection", None)
    if callable(collection_method):
        return bool(collection_method(source))
    generic_method = getattr(resolver, "supports", None)
    return bool(generic_method(source)) if callable(generic_method) else False


async def _resolve(
    resolver: CollectionResolver,
    source: SourceRef,
    options: dict[str, Any],
) -> Collection:
    method: Any = getattr(resolver, "resolve_collection", None)
    if not callable(method):
        method = getattr(resolver, "resolve", None)
    if not callable(method):
        raise UnsupportedSource(
            "collection resolver has no resolve method",
            context={"resolver_id": getattr(resolver, "id", "")},
        )
    result = await method(source, options)
    if not isinstance(result, Collection):
        raise UnsupportedSource(
            "resolver did not return a Collection",
            context={"resolver_id": getattr(resolver, "id", "")},
        )
    return result


def _select(
    collection: Collection,
    options: CollectionPlanOptions,
) -> tuple[tuple[CollectionItem, ...], tuple[str, ...]]:
    total = len(collection.items)
    warnings = list(collection.warnings)
    available = {item.position for item in collection.items}
    positions: set[int]

    requested_items = (
        options.items if isinstance(options.items, tuple) else parse_positions(options.items)
    )
    if requested_items:
        missing = sorted(set(requested_items) - available)
        if missing:
            raise DomainValidationError(
                "playlist item positions are outside the collection",
                user_action="Run plan to list the available positions.",
                context={"positions": missing, "available": sorted(available)},
            )
        positions = set(requested_items)
    else:
        positions = set(available)

    if options.start is not None:
        positions = {position for position in positions if position >= options.start}
    if options.end is not None:
        positions = {position for position in positions if position <= options.end}
    positions -= set(options.skip)

    if not positions:
        raise DomainValidationError(
            "collection selection is empty",
            user_action="Adjust --playlist-start/--playlist-end/--playlist-items/--skip.",
            context={"total_items": total},
        )
    if len(positions) > options.max_items:
        raise CollectionTooLarge(
            f"collection selection has {len(positions)} items; maximum is {options.max_items}",
            context={
                "selected_items": len(positions),
                "max_items": options.max_items,
            },
        )
    if total > MAX_COLLECTION_ITEMS and len(positions) <= options.max_items:
        warnings.append(
            f"collection_limited_by_filter: {total} enumerated; {len(positions)} selected"
        )
    selected = tuple(item for item in collection.items if item.position in positions)
    return selected, tuple(dict.fromkeys(warnings))


def _with_basis(collection: Collection, basis: TagBasis) -> Collection:
    return replace(collection, tag_basis=basis)


def _estimate(collection: Collection, selected: Sequence[CollectionItem]) -> Cost:
    network = isinstance(collection.source_ref, Url)
    per_item = (
        Cost(cpu_seconds=1.0, memory_mb=64.0, network_calls=1)
        if network
        else Cost(cpu_seconds=0.1, memory_mb=64.0)
    )
    total = Cost()
    for _ in selected:
        total = total + per_item
    return total


__all__ = ["PlanCollection"]
