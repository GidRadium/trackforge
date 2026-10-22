"""Domain factories with no database or filesystem dependencies."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import pytest

from trackforge.domain.model.claims import Claim, Observation, Subject
from trackforge.domain.model.decisions import Decision, DecisionRecord
from trackforge.domain.model.ids import (
    ClaimId,
    DecisionRecordId,
    IntakeId,
    ItemId,
    MediaId,
    ObservationId,
    new_uuid7,
)
from trackforge.domain.model.item import Item

NOW = datetime(2025, 1, 1, 12, 0, tzinfo=UTC)


@pytest.fixture
def make_item() -> Callable[..., Item]:
    def _make(**overrides: Any) -> Item:
        item_id = overrides.pop("id", ItemId(new_uuid7()))
        data: dict[str, Any] = {
            "id": item_id,
            "media_id": MediaId(new_uuid7()),
            "primary_intake_id": IntakeId(new_uuid7()),
            "created_at": NOW,
        }
        data.update(overrides)
        return Item(**data)

    return _make


@pytest.fixture
def make_observation() -> Callable[..., Observation]:
    def _make(**overrides: Any) -> Observation:
        data: dict[str, Any] = {
            "id": ObservationId(new_uuid7()),
            "item_id": ItemId(new_uuid7()),
            "type": "audio.probe",
            "schema_version": 1,
            "producer": "fixture/v1",
            "created_at": NOW,
        }
        data.update(overrides)
        return Observation(**data)

    return _make


@pytest.fixture
def make_claim() -> Callable[..., Claim]:
    def _make(**overrides: Any) -> Claim:
        data: dict[str, Any] = {
            "id": ClaimId(new_uuid7()),
            "item_id": ItemId(new_uuid7()),
            "subject": Subject.ITEM,
            "field": "title",
            "value": "Song",
            "source": "fixture",
            "method": "fixture/v1",
            "confidence": 0.9,
            "evidence": ("artifact-1",),
            "created_at": NOW,
        }
        data.update(overrides)
        return Claim(**data)

    return _make


@pytest.fixture
def make_decision_record(
    make_claim: Callable[..., Claim],
) -> Callable[..., DecisionRecord]:
    def _make(**overrides: Any) -> DecisionRecord:
        item_id = overrides.pop("item_id", ItemId(new_uuid7()))
        claim = overrides.pop("claim", make_claim(item_id=item_id))
        decision = overrides.pop(
            "decision",
            Decision(
                field="title",
                subject=Subject.ITEM,
                value=claim.value,
                chosen_claim_id=claim.id,
                alternatives=(claim.id,),
                policy_id="title@1",
                confidence=0.9,
                rationale="fixture decision",
                created_at=NOW,
            ),
        )
        data: dict[str, Any] = {
            "id": DecisionRecordId(new_uuid7()),
            "item_id": item_id,
            "version": 1,
            "decisions": (decision,),
            "policy_versions": ("title@1",),
            "input_claim_ids": (claim.id,),
            "created_at": NOW,
        }
        data.update(overrides)
        return DecisionRecord(**data)

    return _make
