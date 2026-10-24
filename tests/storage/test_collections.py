"""SQLite collection repository round-trip and resume-state tests."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from trackforge.adapters.persistence.sqlite.unit_of_work import SqliteUnitOfWork
from trackforge.domain.errors import DomainValidationError
from trackforge.domain.model.collection import (
    Collection,
    CollectionContextClaim,
    CollectionItem,
    CollectionItemStatus,
    CollectionKind,
    TagBasis,
)
from trackforge.domain.model.ids import CandidateId, CollectionId, new_uuid7
from trackforge.domain.model.item import Item
from trackforge.domain.model.source import Candidate, Format, LocalPath

pytestmark = pytest.mark.integration


def make_collection() -> Collection:
    folder = LocalPath("/music/Artist/Album")
    items = tuple(
        CollectionItem(
            position=index,
            candidate=Candidate(
                id=CandidateId(new_uuid7()),
                source_ref=LocalPath(f"/music/Artist/Album/{index:02d}.mp3"),
                external_id=str(index),
                title=f"Track {index}",
                formats=(Format(container="mp3"),),
            ),
        )
        for index in (1, 2)
    )
    return Collection(
        id=CollectionId(new_uuid7()),
        kind=CollectionKind.FOLDER,
        source_ref=folder,
        title="Album",
        owner="Artist",
        context_claims=(CollectionContextClaim(field="album", value="Album"),),
        items=items,
        tag_basis=TagBasis.PLAYLIST_FIRST,
    )


@pytest.mark.asyncio
async def test_collection_roundtrip_and_item_state_is_preserved(
    uow_factory: Callable[[], SqliteUnitOfWork],
    make_item: Callable[..., Item],
) -> None:
    collection = make_collection()
    item = make_item()

    async with uow_factory() as uow:
        await uow.items.save(item)
        saved = await uow.collections.save(collection)

    assert saved.id == collection.id
    assert saved.tag_basis is TagBasis.PLAYLIST_FIRST
    assert saved.context_claims[0].value == "Album"

    async with uow_factory() as uow:
        loaded = await uow.collections.get(collection.id)
        by_source = await uow.collections.find_by_source(collection.source_ref)
        all_collections = await uow.collections.list_all()
    assert loaded is not None
    assert loaded.id == collection.id
    assert loaded.title == collection.title
    assert [item.position for item in loaded.items] == [1, 2]
    assert by_source is not None and by_source.id == collection.id
    assert [value.id for value in all_collections] == [collection.id]

    async with uow_factory() as uow:
        updated = await uow.collections.update_item(
            collection.id,
            1,
            status=CollectionItemStatus.ACQUIRED,
            item_id=item.id,
        )
    assert updated.items[0].status is CollectionItemStatus.ACQUIRED
    assert updated.items[0].item_id == item.id

    # Re-planning the same source must not downgrade acquired/processed state.
    async with uow_factory() as uow:
        replanned = await uow.collections.save(collection)
    assert replanned.items[0].status is CollectionItemStatus.ACQUIRED
    assert replanned.items[0].item_id == item.id
    assert replanned.items[1].status is CollectionItemStatus.PLANNED


@pytest.mark.asyncio
async def test_duplicate_position_is_rejected_by_domain() -> None:
    collection = make_collection()
    duplicate = CollectionItem(
        position=1,
        candidate=collection.items[0].candidate,
    )
    with pytest.raises(DomainValidationError, match="positions must be unique"):
        Collection(
            id=CollectionId(new_uuid7()),
            kind=CollectionKind.PLAYLIST,
            source_ref=collection.source_ref,
            title="Duplicate",
            items=(collection.items[0], duplicate),
        )
