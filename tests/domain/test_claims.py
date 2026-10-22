"""Observation and claim invariant tests."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime
from typing import Any

import pytest

from trackforge.domain.errors import DomainValidationError
from trackforge.domain.model.claims import (
    Claim,
    ClaimStatus,
    Confidence,
    Observation,
    Provenance,
    Subject,
)

NOW = datetime(2025, 1, 1, 12, 0, tzinfo=UTC)


def test_valid_claim(make_claim: Callable[..., Claim]) -> None:
    claim = make_claim()

    assert claim.subject is Subject.ITEM
    assert claim.status is ClaimStatus.PROPOSED
    assert claim.confidence == 0.9
    assert claim.evidence == ("artifact-1",)


def test_claim_missing_required_fields_is_typed_error() -> None:
    from trackforge.domain.model.ids import ClaimId, ItemId, new_uuid7

    with pytest.raises(DomainValidationError):
        Claim(
            id=ClaimId(new_uuid7()),
            item_id=ItemId(new_uuid7()),
            subject=Subject.ITEM,
            field="title",
            value="Song",
        )


def test_claim_accepts_unknown_value(make_claim: Callable[..., Claim]) -> None:
    claim = make_claim(value=None)

    assert claim.value is None
    assert claim.is_unknown


def test_claim_accepts_list_value(make_claim: Callable[..., Claim]) -> None:
    claim = make_claim(value=["Artist A", "Artist B"])

    assert claim.value == ["Artist A", "Artist B"]


@pytest.mark.parametrize(
    "overrides",
    [
        {"source": ""},
        {"method": ""},
        {"confidence": 1.01},
        {"confidence": -0.01},
        {"confidence": True},
        {"subject": None},
        {"subject": ""},
        {"field": ""},
        {"value": {"not": "canonical"}},
        {"evidence": ()},
    ],
)
def test_claim_invariants(
    make_claim: Callable[..., Claim],
    overrides: dict[str, Any],
) -> None:
    with pytest.raises(DomainValidationError):
        make_claim(**overrides)


def test_claim_evidence_reason_allows_source_snapshot(
    make_claim: Callable[..., Claim],
) -> None:
    claim = make_claim(
        evidence=(),
        evidence_reason="source snapshot is the only evidence",
    )

    assert claim.evidence == ()
    assert claim.evidence_reason is not None


def test_claim_notes_can_explain_source_snapshot(
    make_claim: Callable[..., Claim],
) -> None:
    claim = make_claim(evidence=(), notes="source snapshot")

    assert claim.evidence == ()


def test_claim_cannot_derive_from_itself(
    make_claim: Callable[..., Claim],
) -> None:
    first = make_claim()

    with pytest.raises(DomainValidationError):
        make_claim(id=first.id, derived_from=(first.id,))


def test_synthesized_claim_carries_derived_from(
    make_claim: Callable[..., Claim],
) -> None:
    source = make_claim()
    synthesized = make_claim(
        value="Song (Nightcore)",
        derived_from=(source.id,),
    )

    assert synthesized.is_synthesized
    assert synthesized.derived_from == (source.id,)


def test_provenance_requires_source_and_method() -> None:
    with pytest.raises(DomainValidationError):
        Provenance("", "method", NOW)
    with pytest.raises(DomainValidationError):
        Provenance("source", "", NOW)


def test_provenance_accepts_cost_and_evidence() -> None:
    provenance = Provenance(
        "fixture",
        "fixture/v1",
        NOW,
        evidence=("artifact-1",),
    )

    assert provenance.source == "fixture"
    assert provenance.evidence == ("artifact-1",)


def test_confidence_value_object() -> None:
    assert Confidence(0.25) == 0.25
    assert isinstance(Confidence(0.25), float)

    with pytest.raises(DomainValidationError):
        Confidence(1.5)
    with pytest.raises(DomainValidationError):
        Confidence(-0.1)


def test_observation_is_immutable(make_observation: Callable[..., Observation]) -> None:
    observation = make_observation()

    with pytest.raises(FrozenInstanceError):
        observation.type = "changed"  # type: ignore[misc]


def test_observation_payload_is_deeply_immutable(
    make_observation: Callable[..., Observation],
) -> None:
    observation = make_observation(payload={"tags": ["a"]})

    with pytest.raises(TypeError):
        observation.payload["tags"] = []  # type: ignore[index]


def test_observation_artifact_type(make_observation: Callable[..., Observation]) -> None:
    observation = make_observation(type="audio.features", schema_version=2)

    assert observation.artifact_type == "audio.features.v2"


def test_observation_rejects_invalid_schema_version(
    make_observation: Callable[..., Observation],
) -> None:
    with pytest.raises(DomainValidationError):
        make_observation(schema_version=0)


def test_observation_rejects_non_json_payload(
    make_observation: Callable[..., Observation],
) -> None:
    with pytest.raises(DomainValidationError):
        make_observation(payload={"audio": b"raw bytes"})
