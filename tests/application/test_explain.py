"""Explain use case tests."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from tests.application.conftest import ClaimSpec

from trackforge.bootstrap.container import CatalogContext
from trackforge.domain.errors import RecordNotFound, ResolutionFailed
from trackforge.domain.model.claims import Subject
from trackforge.domain.model.decisions import DecisionMode
from trackforge.domain.model.ids import ItemId

pytestmark = pytest.mark.asyncio


async def _resolve_sample(
    catalog_context: CatalogContext,
    create_item: Callable[..., Any],
    append_claim: Callable[..., Any],
) -> tuple[ItemId, Any]:
    item_id = await create_item()
    chosen = await append_claim(
        item_id,
        ClaimSpec(field="title", value="Song", source="initial_tags", confidence=0.4),
    )
    await append_claim(
        item_id,
        ClaimSpec(field="title", value="Song!", source="path", confidence=0.2),
    )
    await append_claim(item_id, ClaimSpec(field="artist", value="Artist"))
    resolved = await catalog_context.resolve_item().resolve(item_id)
    return item_id, (chosen, resolved)


async def test_explain_title_shows_chosen_claim_and_rejected_alternatives(
    catalog_context: CatalogContext,
    create_item: Callable[..., Any],
    append_claim: Callable[..., Any],
) -> None:
    item_id, (chosen, resolved) = await _resolve_sample(
        catalog_context,
        create_item,
        append_claim,
    )

    view = await catalog_context.explain_item().explain(item_id, "title")

    assert view.field == "title"
    assert view.decision == "Song"
    assert view.value == "Song"
    assert view.unknown is False
    assert view.mode is DecisionMode.POLICY
    assert view.policy_id == "title"
    assert view.policy_version == 1
    assert view.chosen_claim is not None
    assert view.chosen_claim.id == chosen.id
    assert view.chosen_claim.source == "initial_tags"
    assert view.chosen_claim.method == "test_fixture/v1"
    assert view.alternatives
    alternative = next(item for item in view.alternatives if item.source == "path")
    assert alternative.claim_id
    assert alternative.confidence == 0.2
    assert alternative.reason
    assert view.rationale
    assert view.decision_record_id == resolved.decision_record_id
    assert view.record_version == 1
    assert view.locked is False


async def test_explain_unknown_field_keeps_unknown_and_warns(
    catalog_context: CatalogContext,
    create_item: Callable[..., Any],
    append_claim: Callable[..., Any],
) -> None:
    item_id = await create_item()
    await append_claim(item_id, ClaimSpec(field="title", value="Song"))
    await catalog_context.resolve_item().resolve(item_id)

    view = await catalog_context.explain_item().explain(item_id, "album")

    assert view.field == "album"
    assert view.decision is None
    assert view.unknown is True
    assert view.chosen_claim is None
    assert view.alternatives == ()
    assert any("unknown is a valid decision" in warning for warning in view.warnings)


async def test_explain_derived_title_lists_derivation_claims(
    catalog_context: CatalogContext,
    create_item: Callable[..., Any],
    append_claim: Callable[..., Any],
) -> None:
    item_id = await create_item()
    base = await append_claim(item_id, ClaimSpec(field="title", value="Song"))
    transformation = await append_claim(
        item_id,
        ClaimSpec(
            subject=Subject.DERIVATIVE,
            field="transformation",
            value="nightcore",
            confidence=0.6,
        ),
    )
    await catalog_context.resolve_item().resolve(item_id)

    view = await catalog_context.explain_item().explain(item_id, "title")

    assert view.decision == "Song (Nightcore)"
    assert view.policy_id == "title.derivative"
    assert view.chosen_claim is not None
    assert view.chosen_claim.id == base.id
    assert {claim.id for claim in view.derived_from} == {transformation.id}
    assert view.record_version == 1


async def test_explain_lock_shows_user_claim_and_locked_flag(
    catalog_context: CatalogContext,
    create_item: Callable[..., Any],
    append_claim: Callable[..., Any],
) -> None:
    item_id = await create_item()
    await append_claim(item_id, ClaimSpec(field="title", value="Original"))
    await catalog_context.resolve_item().resolve(item_id)
    async with catalog_context.unit_of_work() as uow:
        await uow.locks.upsert(item_id, "title", "Locked Title")
    await catalog_context.resolve_item().resolve(item_id)

    view = await catalog_context.explain_item().explain(item_id, "title")

    assert view.decision == "Locked Title"
    assert view.locked is True
    assert view.record_version == 2
    assert view.chosen_claim is not None
    assert view.chosen_claim.source == "user"
    assert view.chosen_claim.confidence == 1.0


async def test_explain_without_decision_record_raises(
    catalog_context: CatalogContext,
    create_item: Callable[..., Any],
) -> None:
    item_id = await create_item()

    with pytest.raises(ResolutionFailed):
        await catalog_context.explain_item().explain(item_id, "title")


async def test_explain_unknown_field_raises_record_not_found(
    catalog_context: CatalogContext,
    create_item: Callable[..., Any],
    append_claim: Callable[..., Any],
) -> None:
    item_id = await create_item()
    await append_claim(item_id, ClaimSpec(field="title", value="Song"))
    await catalog_context.resolve_item().resolve(item_id)

    with pytest.raises(RecordNotFound):
        await catalog_context.explain_item().explain(item_id, "not_a_field")


async def test_explain_can_select_an_explicit_record(
    catalog_context: CatalogContext,
    create_item: Callable[..., Any],
    append_claim: Callable[..., Any],
) -> None:
    item_id = await create_item()
    await append_claim(item_id, ClaimSpec(field="title", value="Version One"))
    first = await catalog_context.resolve_item().resolve(item_id)
    async with catalog_context.unit_of_work() as uow:
        await uow.locks.upsert(item_id, "title", "Version Two")
    await catalog_context.resolve_item().resolve(item_id)

    view = await catalog_context.explain_item().explain(
        item_id,
        "title",
        record_id=first.decision_record_id,
    )

    assert view.decision == "Version One"
    assert view.record_version == 1
