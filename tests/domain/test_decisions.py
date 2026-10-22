"""Field policy, decision, and decision-record tests."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import pytest

from trackforge.domain.errors import DomainValidationError
from trackforge.domain.model.claims import Subject
from trackforge.domain.model.decisions import (
    DecidedBy,
    Decision,
    DecisionMode,
    DecisionRecord,
    FieldPolicy,
    FieldValueClass,
    OnMissing,
    PolicyScope,
    PolicyStrategy,
)
from trackforge.domain.model.ids import ClaimId, DecisionRecordId, ItemId, new_uuid7

NOW = datetime(2025, 1, 1, 12, 0, tzinfo=UTC)


def _claim_id() -> ClaimId:
    return ClaimId(new_uuid7())


def _decision(**overrides: Any) -> Decision:
    data: dict[str, Any] = {
        "field": "title",
        "subject": Subject.ITEM,
        "value": "Song",
        "policy_id": "title@1",
        "confidence": 0.9,
        "rationale": "source priority",
        "chosen_claim_id": _claim_id(),
        "created_at": NOW,
    }
    data.update(overrides)
    return Decision(**data)


def test_decision_missing_confidence_is_typed_error() -> None:
    with pytest.raises(DomainValidationError):
        Decision(
            field="title",
            subject=Subject.ITEM,
            value="Song",
            policy_id="title@1",
            rationale="source priority",
            chosen_claim_id=_claim_id(),
        )


def test_decision_with_chosen_claim() -> None:
    claim_id = _claim_id()
    decision = _decision(chosen_claim_id=claim_id, alternatives=(claim_id,))

    assert decision.chosen_claim_id == claim_id
    assert not decision.is_unknown
    assert not decision.is_synthesized


def test_unknown_is_a_valid_decision() -> None:
    alternatives = (_claim_id(), _claim_id())
    decision = _decision(
        value=None,
        chosen_claim_id=None,
        alternatives=alternatives,
        confidence=0.4,
        rationale="no evidence above threshold",
    )

    assert decision.value is None
    assert decision.is_unknown
    assert decision.alternatives == alternatives
    assert decision.rationale


def test_unknown_cannot_choose_a_claim() -> None:
    with pytest.raises(DomainValidationError):
        _decision(value=None, chosen_claim_id=_claim_id())


def test_synthesized_value_requires_derived_from() -> None:
    with pytest.raises(DomainValidationError):
        _decision(
            value="Song (Nightcore)",
            chosen_claim_id=None,
            derived_from=(),
        )


def test_synthesized_value_with_derived_from() -> None:
    first = _claim_id()
    second = _claim_id()
    decision = _decision(
        value="Song (Nightcore)",
        chosen_claim_id=None,
        derived_from=(first, second),
        alternatives=(first, second),
    )

    assert decision.is_synthesized
    assert decision.derived_from == (first, second)


def test_rationale_is_required() -> None:
    with pytest.raises(DomainValidationError):
        _decision(rationale="")
    with pytest.raises(DomainValidationError):
        _decision(rationale="   ")


def test_locked_decision_still_requires_rationale() -> None:
    with pytest.raises(DomainValidationError):
        _decision(locked=True, rationale="")


def test_locked_decision_is_valid_with_rationale() -> None:
    decision = _decision(locked=True, rationale="user locked this value")

    assert decision.locked is True


@pytest.mark.parametrize("confidence", [-0.1, 1.1, True])
def test_decision_confidence_bounds(confidence: object) -> None:
    with pytest.raises(DomainValidationError):
        _decision(confidence=confidence)


def test_decision_modes_and_deciders() -> None:
    decision = _decision(
        mode=DecisionMode.LLM_ASSISTED,
        decided_by=DecidedBy.POLICY_LLM,
    )

    assert decision.mode is DecisionMode.LLM_ASSISTED
    assert decision.decided_by is DecidedBy.POLICY_LLM


def test_field_policy_contract() -> None:
    policy = FieldPolicy(
        field="title",
        scope=PolicyScope.ITEM,
        conditions=["kind=nightcore", {"source": "youtube"}],
        strategy=PolicyStrategy.SOURCE_PRIORITY,
        priority=["user", "musicbrainz"],
        validators=["non_empty"],
        on_missing=OnMissing.REVIEW,
        version=2,
    )

    assert policy.strategy is PolicyStrategy.SOURCE_PRIORITY
    assert policy.on_missing is OnMissing.REVIEW
    assert policy.version == 2


def test_field_policy_rejects_invalid_version() -> None:
    with pytest.raises(DomainValidationError):
        FieldPolicy(field="title", version=0)


def test_decision_record_round_trip(
    make_decision_record: Callable[..., DecisionRecord],
) -> None:
    record = make_decision_record()

    assert record.version == 1
    assert record.decision_for("title") is record.decisions[0]
    assert record.decision_for("missing") is None


def test_decision_record_version_must_be_positive(
    make_decision_record: Callable[..., DecisionRecord],
) -> None:
    with pytest.raises(DomainValidationError):
        make_decision_record(version=0)


def test_decision_record_rejects_duplicate_subject_field() -> None:
    first = _decision()
    second = _decision()
    with pytest.raises(DomainValidationError):
        DecisionRecord(
            id=DecisionRecordId(new_uuid7()),
            item_id=ItemId(new_uuid7()),
            version=1,
            decisions=(first, second),
            created_at=NOW,
        )


def test_field_value_class() -> None:
    assert FieldValueClass.of("text") is FieldValueClass.STRING
    assert FieldValueClass.of(3) is FieldValueClass.INTEGER
    assert FieldValueClass.of(3.5) is FieldValueClass.FLOAT
    assert FieldValueClass.of(True) is FieldValueClass.BOOLEAN
    assert FieldValueClass.of(["a"]) is FieldValueClass.STRING_LIST
    assert FieldValueClass.of(None) is FieldValueClass.UNKNOWN
