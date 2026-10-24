"""Policy strategy tests: priority, consensus, derive, unknown, and locks."""

from __future__ import annotations

from collections.abc import Callable

from trackforge.domain.model.claims import Claim, Subject
from trackforge.domain.model.identity import Transformation
from trackforge.domain.model.ids import ItemId, new_uuid7
from trackforge.domain.policies.strategies import (
    select_consensus,
    select_derive,
    select_keep_existing,
    select_review,
    select_source_priority,
    select_unknown,
    select_user_lock,
)


def _item_id() -> ItemId:
    return ItemId(new_uuid7())


def test_source_priority_prefers_configured_source(
    make_claim: Callable[..., Claim],
) -> None:
    item_id = _item_id()
    tag = make_claim(item_id=item_id, value="Song", source="initial_tags", confidence=0.4)
    path = make_claim(item_id=item_id, value="Song!", source="path", confidence=0.2)

    result = select_source_priority(
        "title",
        [path, tag],
        ("user_lock", "item.title", "path.title", "unknown"),
    )

    assert result.value == "Song"
    assert result.selected == (tag,)
    assert tag.id in result.chosen_claim_ids
    assert path.id in {claim.id for claim in result.alternatives}
    assert "source priority" in result.rationale


def test_source_priority_falls_back_to_confidence(
    make_claim: Callable[..., Claim],
) -> None:
    item_id = _item_id()
    low = make_claim(item_id=item_id, value="Low", source="fixture", confidence=0.2)
    high = make_claim(item_id=item_id, value="High", source="fixture", confidence=0.7)

    result = select_source_priority("title", [low, high], ("path.title",))

    assert result.value == "High"
    assert result.selected == (high,)
    assert "highest-confidence" in result.rationale


def test_source_priority_groups_normalized_duplicates(
    make_claim: Callable[..., Claim],
) -> None:
    item_id = _item_id()
    first = make_claim(item_id=item_id, value="Song!", source="initial_tags", confidence=0.4)
    second = make_claim(item_id=item_id, value="Song", source="initial_tags", confidence=0.3)

    result = select_source_priority("title", [first, second], ("item.title",))

    assert result.value == "Song!"
    assert result.selected == (first,)
    assert result.alternatives == (second,)


def test_keep_existing_prefers_existing_tag(
    make_claim: Callable[..., Claim],
) -> None:
    item_id = _item_id()
    existing = make_claim(item_id=item_id, value="Old", source="initial_tags", confidence=0.3)
    new = make_claim(item_id=item_id, value="New", source="source_metadata", confidence=0.9)

    result = select_keep_existing("comment", [new, existing], ("source_url",))

    assert result.value == "Old"
    assert result.selected == (existing,)
    assert new.id in {claim.id for claim in result.alternatives}


def test_consensus_boosts_independent_sources(
    make_claim: Callable[..., Claim],
) -> None:
    item_id = _item_id()
    first = make_claim(item_id=item_id, value="Song", source="source_a", confidence=0.6)
    second = make_claim(item_id=item_id, value="Song!", source="source_b", confidence=0.55)
    conflict = make_claim(item_id=item_id, value="Other", source="source_c", confidence=0.65)

    result = select_consensus("title", [first, second, conflict])

    assert result.value == "Song"
    assert result.confidence == 0.7
    assert conflict.id in {claim.id for claim in result.alternatives}


def test_consensus_does_not_boost_same_source(
    make_claim: Callable[..., Claim],
) -> None:
    item_id = _item_id()
    first = make_claim(item_id=item_id, value="Song", source="source_a", confidence=0.6)
    second = make_claim(item_id=item_id, value="Song", source="source_a", confidence=0.6)

    result = select_consensus("title", [first, second])

    assert result.confidence == 0.6


def test_derive_appends_qualifier_once(
    make_claim: Callable[..., Claim],
) -> None:
    item_id = _item_id()
    base_claim = make_claim(item_id=item_id, value="Song", source="initial_tags", confidence=0.4)
    transformation = make_claim(
        item_id=item_id,
        subject=Subject.DERIVATIVE,
        field="transformation",
        value=Transformation.NIGHTCORE.value,
        source="initial_tags",
        confidence=0.65,
    )
    base = select_source_priority("title", [base_claim], ("item.title",))

    derived = select_derive("title", base, [transformation])
    again = select_derive("title", derived, [transformation])

    assert derived.value == "Song (Nightcore)"
    assert derived.synthesized is False
    assert derived.chosen_claim_id == base_claim.id
    assert transformation.id in derived.derived_from
    assert again.value == "Song (Nightcore)"
    assert "not duplicated" in again.rationale


def test_derive_keeps_title_that_already_has_qualifier(
    make_claim: Callable[..., Claim],
) -> None:
    item_id = _item_id()
    base_claim = make_claim(
        item_id=item_id,
        value="Song (Nightcore)",
        source="initial_tags",
    )
    transformation = make_claim(
        item_id=item_id,
        subject=Subject.DERIVATIVE,
        field="transformation",
        value="nightcore",
        source="initial_tags",
    )
    base = select_source_priority("title", [base_claim], ("item.title",))

    result = select_derive("title", base, [transformation])

    assert result.value == "Song (Nightcore)"
    assert result.synthesized is False


def test_unknown_and_review_are_explicit(
    make_claim: Callable[..., Claim],
) -> None:
    item_id = _item_id()
    claim = make_claim(item_id=item_id, value="Maybe", source="fixture")

    unknown = select_unknown("title", [claim], reason="below threshold")
    review = select_review("title", [claim], reason="needs a human")

    assert unknown.value is None
    assert unknown.selected == ()
    assert claim.id in {item.id for item in unknown.alternatives}
    assert unknown.review_required is False
    assert review.value is None
    assert review.review_required is True


def test_user_lock_wins_and_is_marked_locked(
    make_claim: Callable[..., Claim],
) -> None:
    item_id = _item_id()
    user_claim = make_claim(
        item_id=item_id,
        value="Locked",
        source="user",
        method="manual_override/v1",
        confidence=1.0,
    )
    other = make_claim(item_id=item_id, value="Other", source="initial_tags")

    result = select_user_lock(
        "title",
        "Locked",
        [user_claim, other],
        user_claim=user_claim,
    )

    assert result.value == "Locked"
    assert result.locked is True
    assert result.confidence == 1.0
    assert result.selected == (user_claim,)
    assert other.id in {claim.id for claim in result.alternatives}


def test_derive_normalizes_qualifier_prefix(make_claim) -> None:
    from trackforge.domain.model.claims import Subject
    from trackforge.domain.model.identity import Transformation
    from trackforge.domain.policies.strategies import StrategyResult

    source = make_claim(
        field="title",
        value="Nightcore | Queen of Shadow",
        subject=Subject.ITEM,
        source="source_metadata",
        method="source_metadata/v1",
        confidence=0.75,
    )
    transformation = make_claim(
        field="transformation",
        value=Transformation.NIGHTCORE.value,
        subject=Subject.DERIVATIVE,
        source="source_metadata",
        method="derivative_detector/v1",
        confidence=0.8,
    )
    base = StrategyResult(
        field="title",
        value=source.value,
        selected=(source,),
        confidence=0.75,
        rationale="test base",
    )

    derived = select_derive("title", base, [transformation])

    assert derived.value == "Queen of Shadow (Nightcore)"
