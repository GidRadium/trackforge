"""Resolve use case tests: policies, locks, unknown, derive, and modes."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from tests.application.conftest import ClaimSpec

from trackforge.bootstrap.container import CatalogContext
from trackforge.domain.errors import DomainValidationError, ResolutionFailed, UnsupportedMode
from trackforge.domain.model.claims import Subject
from trackforge.domain.model.decisions import FieldPolicy, OnMissing, PolicyStrategy
from trackforge.domain.model.ids import ItemId
from trackforge.domain.model.item import ItemStatus

pytestmark = pytest.mark.asyncio


async def _title(
    item_id: ItemId,
    append_claim: Callable[..., Any],
    value: object = "Song",
    *,
    source: str = "initial_tags",
    confidence: float = 0.4,
) -> Any:
    return await append_claim(
        item_id,
        ClaimSpec(field="title", value=value, source=source, confidence=confidence),
    )


async def test_resolve_creates_versioned_record_with_decisions(
    catalog_context: CatalogContext,
    create_item: Callable[..., Any],
    append_claim: Callable[..., Any],
) -> None:
    item_id = await create_item()
    tag_title = await _title(item_id, append_claim)
    path_title = await _title(
        item_id,
        append_claim,
        "Song!",
        source="path",
        confidence=0.2,
    )
    await append_claim(item_id, ClaimSpec(field="artist", value="Artist"))
    await append_claim(item_id, ClaimSpec(field="album", value="Album"))
    await append_claim(item_id, ClaimSpec(field="albumartist", value="Artist"))

    resolved = await catalog_context.resolve_item().resolve(item_id)

    assert resolved.fields["title"] == "Song"
    assert resolved.fields["artist"] == "Artist"
    assert resolved.fields["album"] == "Album"

    async with catalog_context.unit_of_work() as uow:
        item = await uow.items.get(item_id)
        record = await uow.decisions.get_record(resolved.decision_record_id)
    assert item is not None
    assert item.status is ItemStatus.RESOLVED
    assert item.decision_record_id == resolved.decision_record_id
    assert record is not None
    assert record.version == 1
    assert "title@1" in record.policy_versions
    assert tag_title.id in record.input_claim_ids
    assert path_title.id in record.input_claim_ids

    title = record.decision_for("title")
    assert title is not None
    assert title.value == "Song"
    assert title.chosen_claim_id == tag_title.id
    assert path_title.id in title.alternatives
    assert title.rationale
    assert title.locked is False


async def test_unknown_is_a_legal_decision_not_an_invented_value(
    catalog_context: CatalogContext,
    create_item: Callable[..., Any],
    append_claim: Callable[..., Any],
) -> None:
    item_id = await create_item()
    await _title(item_id, append_claim)

    resolved = await catalog_context.resolve_item().resolve(item_id)

    async with catalog_context.unit_of_work() as uow:
        record = await uow.decisions.get_record(resolved.decision_record_id)
    assert record is not None
    album = record.decision_for("album")
    assert resolved.fields["album"] is None
    assert "album" in resolved.unknowns
    assert album is not None
    assert album.value is None
    assert album.chosen_claim_id is None
    assert album.confidence == 0.0
    assert album.rationale


async def test_lock_wins_and_re_resolve_creates_a_new_record(
    catalog_context: CatalogContext,
    create_item: Callable[..., Any],
    append_claim: Callable[..., Any],
) -> None:
    item_id = await create_item()
    original = await _title(item_id, append_claim, "Original")
    first = await catalog_context.resolve_item().resolve(item_id)
    async with catalog_context.unit_of_work() as uow:
        await uow.locks.upsert(item_id, "title", "Locked Title")
        claim_count_before = len(await uow.claims.list_by_item(item_id))

    second = await catalog_context.resolve_item().resolve(item_id)

    assert first.decision_record_id != second.decision_record_id
    assert second.fields["title"] == "Locked Title"
    async with catalog_context.unit_of_work() as uow:
        records = await uow.decisions.list_records(item_id)
        claims = await uow.claims.list_by_item(item_id)
    assert [record.version for record in records] == [1, 2]
    decision = records[1].decision_for("title")
    assert decision is not None
    assert decision.locked is True
    assert decision.value == "Locked Title"
    assert decision.confidence == 1.0
    assert decision.chosen_claim_id is not None
    assert len(claims) == claim_count_before + 1
    assert original.id in {claim.id for claim in claims}
    first_decision = records[0].decision_for("title")
    assert first_decision is not None
    assert first_decision.value == "Original"


async def test_derivative_title_is_derived_and_not_duplicated(
    catalog_context: CatalogContext,
    create_item: Callable[..., Any],
    append_claim: Callable[..., Any],
) -> None:
    item_id = await create_item()
    await _title(item_id, append_claim, "Song")
    transformation = await append_claim(
        item_id,
        ClaimSpec(
            subject=Subject.DERIVATIVE,
            field="transformation",
            value="nightcore",
            confidence=0.6,
        ),
    )
    path_title = await _title(
        item_id,
        append_claim,
        "Song",
        source="path",
        confidence=0.2,
    )

    first = await catalog_context.resolve_item().resolve(item_id)
    second = await catalog_context.resolve_item().resolve(item_id)

    assert first.fields["title"] == "Song (Nightcore)"
    assert second.fields["title"] == "Song (Nightcore)"
    async with catalog_context.unit_of_work() as uow:
        record = await uow.decisions.get_record(second.decision_record_id)
    assert record is not None
    title = record.decision_for("title")
    assert title is not None
    assert title.value == "Song (Nightcore)"
    assert title.policy_id == "title.derivative"
    assert title.chosen_claim_id is not None
    assert transformation.id in title.derived_from
    assert path_title.id in title.alternatives
    assert title.rationale.count("Nightcore") == 1


async def test_title_with_existing_qualifier_is_not_modified(
    catalog_context: CatalogContext,
    create_item: Callable[..., Any],
    append_claim: Callable[..., Any],
) -> None:
    item_id = await create_item()
    await _title(item_id, append_claim, "Song (Nightcore)")
    await append_claim(
        item_id,
        ClaimSpec(
            subject=Subject.DERIVATIVE,
            field="transformation",
            value="nightcore",
            confidence=0.6,
        ),
    )

    resolved = await catalog_context.resolve_item().resolve(item_id)

    assert resolved.fields["title"] == "Song (Nightcore)"


async def test_all_invalid_claims_resolve_to_unknown(
    catalog_context: CatalogContext,
    create_item: Callable[..., Any],
    append_claim: Callable[..., Any],
) -> None:
    item_id = await create_item()
    invalid = await _title(item_id, append_claim, "   ")

    resolved = await catalog_context.resolve_item().resolve(item_id)

    async with catalog_context.unit_of_work() as uow:
        record = await uow.decisions.get_record(resolved.decision_record_id)
    assert record is not None
    title = record.decision_for("title")
    assert resolved.fields["title"] is None
    assert title is not None
    assert title.value is None
    assert invalid.id in title.alternatives


async def test_ids_are_unknown_in_policy_mode_even_with_valid_claims(
    catalog_context: CatalogContext,
    create_item: Callable[..., Any],
    append_claim: Callable[..., Any],
) -> None:
    item_id = await create_item()
    mbid = await append_claim(
        item_id,
        ClaimSpec(
            field="mb_recording_id",
            value="11111111-1111-1111-1111-111111111111",
            confidence=0.3,
        ),
    )

    resolved = await catalog_context.resolve_item().resolve(item_id)

    async with catalog_context.unit_of_work() as uow:
        record = await uow.decisions.get_record(resolved.decision_record_id)
    assert record is not None
    decision = record.decision_for("mb_recording_id")
    assert resolved.fields["mb_recording_id"] is None
    assert decision is not None
    assert decision.value is None
    assert mbid.id in decision.alternatives
    assert "authority" in decision.rationale
    assert any("authority evidence" in warning for warning in resolved.warnings)


@pytest.mark.parametrize(
    "mode",
    ["llm_assisted", "llm_autopilot", "manual"],
)
async def test_unsupported_modes_raise(
    catalog_context: CatalogContext,
    create_item: Callable[..., Any],
    append_claim: Callable[..., Any],
    mode: str,
) -> None:
    item_id = await create_item()
    await _title(item_id, append_claim)

    with pytest.raises(UnsupportedMode):
        await catalog_context.resolve_item().resolve(item_id, mode=mode)


async def test_cross_field_track_exceeds_total_becomes_unknown(
    catalog_context: CatalogContext,
    create_item: Callable[..., Any],
    append_claim: Callable[..., Any],
) -> None:
    item_id = await create_item()
    await _title(item_id, append_claim)
    await append_claim(item_id, ClaimSpec(field="track", value=5))
    await append_claim(item_id, ClaimSpec(field="tracktotal", value=3))

    resolved = await catalog_context.resolve_item().resolve(item_id)

    assert resolved.fields["track"] is None
    assert resolved.fields["tracktotal"] == 3
    assert any("exceeds tracktotal" in warning for warning in resolved.warnings)


async def test_lock_on_unknown_field_is_rejected(
    catalog_context: CatalogContext,
    create_item: Callable[..., Any],
) -> None:
    item_id = await create_item()
    async with catalog_context.unit_of_work() as uow:
        await uow.locks.upsert(item_id, "not_a_policy_field", "value")

    with pytest.raises(DomainValidationError):
        await catalog_context.resolve_item().resolve(item_id)


async def test_policy_override_on_missing_review_is_visible(
    catalog_context: CatalogContext,
    create_item: Callable[..., Any],
    append_claim: Callable[..., Any],
) -> None:
    item_id = await create_item()
    await _title(item_id, append_claim)
    policy = FieldPolicy(
        id="album.review",
        field="album",
        strategy=PolicyStrategy.SOURCE_PRIORITY,
        priority=("item.album", "unknown"),
        on_missing=OnMissing.REVIEW,
        version=2,
    )

    resolved = await catalog_context.resolve_item().resolve(
        item_id,
        policy_overrides=policy,
    )

    assert resolved.fields["album"] is None
    assert any("review requested" in warning for warning in resolved.warnings)
    async with catalog_context.unit_of_work() as uow:
        record = await uow.decisions.get_record(resolved.decision_record_id)
    assert record is not None
    album = record.decision_for("album")
    assert album is not None
    assert "review" in album.rationale
    assert "album.review@2" in record.policy_versions


async def test_policy_override_consensus_boosts_confidence(
    catalog_context: CatalogContext,
    create_item: Callable[..., Any],
    append_claim: Callable[..., Any],
) -> None:
    item_id = await create_item()
    await _title(item_id, append_claim, "Song", source="source_a", confidence=0.6)
    await _title(item_id, append_claim, "Song!", source="source_b", confidence=0.55)
    policy = FieldPolicy(
        id="title.consensus",
        field="title",
        strategy=PolicyStrategy.CONSENSUS,
        priority=("unknown",),
        version=1,
    )

    resolved = await catalog_context.resolve_item().resolve(
        item_id,
        policy_overrides={"title": policy},
    )

    assert resolved.fields["title"] == "Song"
    async with catalog_context.unit_of_work() as uow:
        record = await uow.decisions.get_record(resolved.decision_record_id)
    assert record is not None
    title = record.decision_for("title")
    assert title is not None
    assert title.confidence == 0.7
    assert title.policy_id == "title.consensus"


async def test_invalid_lock_value_fails_clearly(
    catalog_context: CatalogContext,
    create_item: Callable[..., Any],
) -> None:
    item_id = await create_item()
    async with catalog_context.unit_of_work() as uow:
        await uow.locks.upsert(item_id, "title", "")

    with pytest.raises(ResolutionFailed):
        await catalog_context.resolve_item().resolve(item_id)
