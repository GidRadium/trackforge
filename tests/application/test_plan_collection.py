"""Collection planning, filters, and hard-limit tests."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest

from trackforge.application.dto.collections import CollectionPlanOptions
from trackforge.application.use_cases.plan_collection import PlanCollection
from trackforge.domain.errors import CollectionTooLarge, DomainValidationError
from trackforge.domain.model.collection import (
    Collection,
    CollectionItem,
    CollectionKind,
    TagBasis,
    validate_collection_title,
)
from trackforge.domain.model.ids import CandidateId, CollectionId, new_uuid7
from trackforge.domain.model.source import Candidate, SourceRef, Url

pytestmark = pytest.mark.unit

URL = Url("https://www.youtube.com/playlist?list=fixture")


def collection(count: int, *, title: str = "Fixture Playlist") -> Collection:
    return Collection(
        id=CollectionId(new_uuid7()),
        kind=CollectionKind.PLAYLIST,
        source_ref=URL,
        title=title,
        items=tuple(
            CollectionItem(
                position=index,
                candidate=Candidate(
                    id=CandidateId(new_uuid7()),
                    source_ref=Url(f"https://www.youtube.com/watch?v={index}"),
                    external_id=str(index),
                    title=f"Track {index}",
                ),
            )
            for index in range(1, count + 1)
        ),
    )


class FakeResolver:
    id = "fixture_collection"
    version = "1"

    def __init__(self, value: Collection) -> None:
        self.value = value

    def supports_collection(self, ref: SourceRef) -> bool:
        return True

    async def resolve_collection(
        self,
        ref: SourceRef,
        opts: Mapping[str, Any] | None = None,
    ) -> Collection:
        del ref, opts
        return self.value


class GenericResolver:
    id = "generic"
    version = "1"

    def __init__(self, value: Collection) -> None:
        self.value = value

    def supports(self, ref: SourceRef) -> bool:
        return True

    async def resolve(
        self,
        ref: SourceRef,
        opts: Mapping[str, Any] | None = None,
    ) -> Collection:
        del ref, opts
        return self.value


@pytest.mark.asyncio
async def test_generic_resolve_spelling_is_supported() -> None:
    planner = PlanCollection([GenericResolver(collection(2))])

    plan = await planner.plan(URL)

    assert [item.position for item in plan.selected_items] == [1, 2]


@pytest.mark.asyncio
async def test_range_items_and_skip_filters() -> None:
    planner = PlanCollection([FakeResolver(collection(6))])

    ranged = await planner.plan(URL, CollectionPlanOptions(start=2, end=4))
    assert [item.position for item in ranged.selected_items] == [2, 3, 4]

    selected = await planner.plan(
        URL,
        CollectionPlanOptions(items="1,3-5", skip="4"),
    )
    assert [item.position for item in selected.selected_items] == [1, 3, 5]
    assert selected.limits == {"max_items": 1000, "total_items": 6, "selected_items": 3}


@pytest.mark.asyncio
async def test_tag_basis_is_carried_into_the_plan() -> None:
    planner = PlanCollection([FakeResolver(collection(2))])

    plan = await planner.plan(
        URL,
        CollectionPlanOptions(tag_basis=TagBasis.PLAYLIST_FIRST),
    )

    assert plan.tag_basis is TagBasis.PLAYLIST_FIRST
    assert plan.collection.tag_basis is TagBasis.PLAYLIST_FIRST


@pytest.mark.asyncio
async def test_more_than_1000_items_is_rejected() -> None:
    planner = PlanCollection([FakeResolver(collection(1001))])

    with pytest.raises(CollectionTooLarge):
        await planner.plan(URL, CollectionPlanOptions())


@pytest.mark.asyncio
async def test_filter_can_select_fewer_than_1000_with_warning() -> None:
    planner = PlanCollection([FakeResolver(collection(1001))])

    plan = await planner.plan(URL, CollectionPlanOptions(start=1, end=1000))

    assert len(plan.selected_items) == 1000
    assert any("limited_by_filter" in warning for warning in plan.warnings)


@pytest.mark.asyncio
async def test_missing_or_empty_selection_is_rejected() -> None:
    planner = PlanCollection([FakeResolver(collection(3))])

    with pytest.raises(DomainValidationError):
        await planner.plan(URL, CollectionPlanOptions(items="9"))
    with pytest.raises(DomainValidationError):
        await planner.plan(URL, CollectionPlanOptions(skip="1-3"))


@pytest.mark.asyncio
async def test_requested_rows_receive_duplicate_selection() -> None:
    planner = PlanCollection([FakeResolver(collection(2))])
    plan = await planner.plan(URL, CollectionPlanOptions(items="2,1,2"))

    assert [item.position for item in plan.selected_items] == [1, 2]


def test_tag_basis_validation_happens_in_options() -> None:
    with pytest.raises(DomainValidationError):
        CollectionPlanOptions(tag_basis="quality_first")


def test_collection_title_rejects_path_traversal() -> None:
    for title in ("../escape", "..\\escape", "..", "/absolute", "a/../b"):
        with pytest.raises(DomainValidationError):
            validate_collection_title(title)
    assert validate_collection_title("AC/DC Live") == "AC/DC Live"
